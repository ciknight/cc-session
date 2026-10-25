"""看板独立的会话偏好与整理元数据，不改 Claude 的配置或转录。"""
import json
import os
import tempfile
import threading
import unicodedata
from pathlib import Path


UNSET = object()

APPROVAL_MODES = ("ask", "auto", "full")

EFFORT_LEVELS = ("low", "medium", "high")


def validate_model(model):
    if model is None:
        return None
    if (not isinstance(model, str) or not model or len(model) > 256
            or model != model.strip() or model.startswith("-")
            or any(unicodedata.category(c).startswith("C") or c.isspace() for c in model)):
        raise ValueError("模型必须为不含空白或控制字符的名称（最多 256 字符），或 null")
    return model


def default_model(args):
    model = None
    for i, arg in enumerate(args):
        if arg == "--model" and i + 1 < len(args):
            model = args[i + 1]
        elif arg.startswith("--model="):
            model = arg.split("=", 1)[1]
    return model


def model_args(args, model):
    if model is None:
        return list(args)
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--model":
            i += 2
            continue
        if not arg.startswith("--model="):
            result.append(arg)
        i += 1
    return result + ["--model=" + validate_model(model)]


def validate_effort(level):
    """effort 接口值：low / medium / high；default 规范化为 None（模型默认）。"""
    if level == "default":
        return None
    if level not in EFFORT_LEVELS:
        raise ValueError("effort 必须是 low / medium / high / default")
    return level


def effort_args(args, effort):
    """有档位时剥掉用户自带的 --effort 再追加，模式与 model_args 一致。"""
    if effort is None:
        return list(args)
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--effort":
            i += 2
            continue
        if not arg.startswith("--effort="):
            result.append(arg)
        i += 1
    return result + ["--effort=" + validate_effort(effort)]


def validate_metadata_patch(patch):
    """校验整理字段的局部更新，返回独立的规范化副本。"""
    if (not isinstance(patch, dict) or not patch
            or set(patch) - {"alias", "pinned", "archived"}):
        raise ValueError("会话元数据必须为仅含 alias / pinned / archived 的非空对象")
    normalized = dict(patch)
    if "alias" in normalized:
        alias = normalized["alias"]
        if alias is not None:
            # 先拒绝控制字符，避免换行、制表符被 strip 静默清除。
            if (not isinstance(alias, str)
                    or any(unicodedata.category(c).startswith("C") for c in alias)):
                raise ValueError("别名必须为不含控制字符的字符串，或 null")
            alias = alias.strip()
            if len(alias) > 120:
                raise ValueError("别名最多 120 字符")
            normalized["alias"] = alias or None
    for field in ("pinned", "archived"):
        if field in normalized and type(normalized[field]) is not bool:
            raise ValueError("pinned / archived 必须为布尔值")
    return normalized


class SessionMetadata:
    """按会话保存别名、置顶与归档，版本与字段一起原子持久化。"""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.sessions = {}
        self.error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or not isinstance(data.get("sessions"), dict)):
                raise ValueError("格式或版本不支持")
            loaded = {}
            for sid, record in data["sessions"].items():
                if (not isinstance(sid, str) or not isinstance(record, dict)
                        or set(record) != {"alias", "pinned", "archived", "revision"}
                        or type(record["revision"]) is not int or record["revision"] < 0):
                    raise ValueError("会话元数据记录无效")
                fields = validate_metadata_patch({key: record[key]
                                                  for key in ("alias", "pinned", "archived")})
                loaded[sid] = dict(fields, revision=record["revision"])
            # 整个文件校验通过才加载，坏记录不能造成部分读取后覆盖原文件。
            self.sessions = loaded
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "会话元数据文件无法读取或格式损坏，请先修复或移走该文件"

    def _snapshot(self, sid):
        record = self.sessions.get(sid, {})
        return {"alias": record.get("alias"), "pinned": record.get("pinned", False),
                "archived": record.get("archived", False),
                "metadata_revision": record.get("revision", 0)}

    def get(self, sid):
        with self.lock:
            return self._snapshot(sid)

    def update(self, sid, patch):
        normalized = validate_metadata_patch(patch)
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            current = self._snapshot(sid)
            record = {key: current[key] for key in ("alias", "pinned", "archived")}
            record.update(normalized)
            record["revision"] = current["metadata_revision"] + 1
            updated = dict(self.sessions)
            updated[sid] = record
            temp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temp = tempfile.mkstemp(prefix="metadata-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "sessions": updated}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
            except OSError as exc:
                raise ValueError("会话元数据保存失败，请检查目录权限或磁盘空间") from exc
            finally:
                if temp:
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass
            # 写入成功后才发布新状态；全部恢复默认也保留递增版本。
            self.sessions = updated
            return self._snapshot(sid)


class ModelPreferences:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.models = {}
        self.touched = set()
        self.error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("models"), dict)):
                raise ValueError("格式或版本不支持")
            for sid, model in data["models"].items():
                if not isinstance(sid, str) or validate_model(model) is None:
                    raise ValueError("会话模型记录无效")
            self.models = data["models"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "模型偏好文件无法读取或格式损坏，请先修复或移走该文件"

    def get(self, sid):
        with self.lock:
            return self.models.get(sid)

    def set(self, sid, model):
        model = validate_model(model)
        with self.lock:
            self._save(sid, model)
            self.touched.add(sid)
        return model

    def bind(self, sid, model):
        """新会话拿到 sid 后关联首轮选择，不覆盖随后手动保存的选择。"""
        with self.lock:
            if sid not in self.models and sid not in self.touched and model is not None:
                self._save(sid, validate_model(model))

    def _save(self, sid, model):
        if self.error:
            raise ValueError(self.error)
        updated = dict(self.models)
        if model is None:
            updated.pop(sid, None)
        else:
            updated[sid] = model
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temp = tempfile.mkstemp(prefix="preferences-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "models": updated}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        except OSError as exc:
            raise ValueError("模型偏好保存失败，请检查目录权限或磁盘空间") from exc
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
        self.models = updated


class EffortPreferences:
    """看板独立的托管思考档位；spawn 带 --effort，使重托管后保持。"""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.efforts = {}
        self.touched = set()
        self.error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("efforts"), dict)):
                raise ValueError("格式或版本不支持")
            for sid, level in data["efforts"].items():
                if not isinstance(sid, str) or level not in EFFORT_LEVELS:
                    raise ValueError("思考档位记录无效")
            self.efforts = data["efforts"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "思考档位文件无法读取或格式损坏，请先修复或移走该文件"

    def get(self, sid):
        with self.lock:
            return self.efforts.get(sid)

    def set(self, sid, level):
        if level is not None and level not in EFFORT_LEVELS:
            raise ValueError("思考档位必须是 low / medium / high，或 null")
        with self.lock:
            self._save(sid, level)
            self.touched.add(sid)
        return level

    def bind(self, sid, level):
        """新会话拿到 sid 后关联首轮选择，不覆盖随后手动保存的选择。"""
        with self.lock:
            if sid not in self.efforts and sid not in self.touched and level is not None:
                self._save(sid, level)

    def _save(self, sid, level):
        if self.error:
            raise ValueError(self.error)
        updated = dict(self.efforts)
        if level is None:
            updated.pop(sid, None)
        else:
            updated[sid] = level
        temp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temp = tempfile.mkstemp(prefix="efforts-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "efforts": updated}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        except OSError as exc:
            raise ValueError("思考档位保存失败，请检查目录权限或磁盘空间") from exc
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
        self.efforts = updated


class ApprovalPreferences:
    """看板独立的托管审批模式，不修改 Claude 配置或转录。"""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.modes = {}
        self.error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("approvals"), dict)):
                raise ValueError("格式或版本不支持")
            for sid, mode in data["approvals"].items():
                if not isinstance(sid, str) or mode not in APPROVAL_MODES:
                    raise ValueError("审批模式记录无效")
            self.modes = data["approvals"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "审批模式文件无法读取或格式损坏，请先修复或移走该文件"

    def get(self, sid, default="auto"):
        with self.lock:
            return self.modes.get(sid, default)

    def set(self, sid, mode):
        if mode not in APPROVAL_MODES:
            raise ValueError("审批模式必须是 ask / auto / full")
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            updated = dict(self.modes)
            updated[sid] = mode
            temp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temp = tempfile.mkstemp(prefix="approvals-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "approvals": updated}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
            except OSError as exc:
                raise ValueError("审批模式保存失败，请检查目录权限或磁盘空间") from exc
            finally:
                if temp and os.path.exists(temp):
                    os.unlink(temp)
            self.modes = updated
            return mode
