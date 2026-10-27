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


def _replace_arg(args, name, value):
    """剥掉 args 里已有的 --name / --name=value 后追加覆盖值；不修改原列表。"""
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == name:
            i += 2
            continue
        if not arg.startswith(name + "="):
            result.append(arg)
        i += 1
    return result + [name + "=" + value]


def model_args(args, model):
    if model is None:
        return list(args)
    return _replace_arg(args, "--model", validate_model(model))


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
    return _replace_arg(args, "--effort", validate_effort(effort))


def _atomic_json_dump(path, payload, prefix, suffix=".tmp"):
    """原子写 JSON：同目录临时文件 + fsync + replace。

    主流程失败时清理临时文件；清理本身失败不能掩盖原始异常。
    """
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass


class _MapPreferences:
    """sid -> 单值偏好的共用实现：加载校验、原子保存与 bind/touched 语义。

    子类声明 field/prefix/suffix 与错误文案，并提供 _record_valid（加载时
    单条记录校验）和 _validate（set/bind 时的值校验）。
    """

    field = ""
    prefix = ""
    suffix = ".tmp"
    load_error = ""
    save_error = ""
    record_error = ""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.values = {}
        self.touched = set()
        self.error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get(self.field), dict)):
                raise ValueError("格式或版本不支持")
            for sid, value in data[self.field].items():
                if not isinstance(sid, str) or not self._record_valid(value):
                    raise ValueError(self.record_error)
            self.values = data[self.field]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = self.load_error

    @staticmethod
    def _record_valid(value):
        raise NotImplementedError

    @staticmethod
    def _validate(value):
        raise NotImplementedError

    def get(self, sid):
        with self.lock:
            return self.values.get(sid)

    def set(self, sid, value):
        value = self._validate(value)
        with self.lock:
            self._save(sid, value)
            self.touched.add(sid)
        return value

    def bind(self, sid, value):
        """新会话拿到 sid 后关联首轮选择，不覆盖随后手动保存的选择。"""
        with self.lock:
            if sid not in self.values and sid not in self.touched and value is not None:
                self._save(sid, self._validate(value))

    def _save(self, sid, value):
        if self.error:
            raise ValueError(self.error)
        updated = dict(self.values)
        if value is None:
            updated.pop(sid, None)
        else:
            updated[sid] = value
        try:
            _atomic_json_dump(self.path, {"version": 1, self.field: updated},
                              self.prefix, self.suffix)
        except OSError as exc:
            raise ValueError(self.save_error) from exc
        self.values = updated


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
            try:
                _atomic_json_dump(self.path, {"version": 1, "sessions": updated},
                                  "metadata-")
            except OSError as exc:
                raise ValueError("会话元数据保存失败，请检查目录权限或磁盘空间") from exc
            # 写入成功后才发布新状态；全部恢复默认也保留递增版本。
            self.sessions = updated
            return self._snapshot(sid)


class ModelPreferences(_MapPreferences):
    field = "models"
    prefix = "preferences-"
    load_error = "模型偏好文件无法读取或格式损坏，请先修复或移走该文件"
    save_error = "模型偏好保存失败，请检查目录权限或磁盘空间"
    record_error = "会话模型记录无效"

    @staticmethod
    def _record_valid(value):
        try:
            return validate_model(value) is not None
        except ValueError:
            return False

    _validate = staticmethod(validate_model)


class EffortPreferences(_MapPreferences):
    """看板独立的托管思考档位；spawn 带 --effort，使重托管后保持。"""

    field = "efforts"
    prefix = "efforts-"
    load_error = "思考档位文件无法读取或格式损坏，请先修复或移走该文件"
    save_error = "思考档位保存失败，请检查目录权限或磁盘空间"
    record_error = "思考档位记录无效"

    @staticmethod
    def _record_valid(value):
        return value in EFFORT_LEVELS

    @staticmethod
    def _validate(level):
        if level is not None and level not in EFFORT_LEVELS:
            raise ValueError("思考档位必须是 low / medium / high，或 null")
        return level


class ApprovalPreferences(_MapPreferences):
    """看板独立的托管审批模式，不修改 Claude 配置或转录。"""

    field = "approvals"
    prefix = "approvals-"
    load_error = "审批模式文件无法读取或格式损坏，请先修复或移走该文件"
    save_error = "审批模式保存失败，请检查目录权限或磁盘空间"
    record_error = "审批模式记录无效"

    @staticmethod
    def _record_valid(value):
        return value in APPROVAL_MODES

    @staticmethod
    def _validate(mode):
        if mode not in APPROVAL_MODES:
            raise ValueError("审批模式必须是 ask / auto / full")
        return mode

    def get(self, sid, default="auto"):
        with self.lock:
            return self.values.get(sid, default)
