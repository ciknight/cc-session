"""看板独立的会话模型偏好，不改 Claude 的配置或转录。"""
import json
import os
import tempfile
import threading
import unicodedata
from pathlib import Path


UNSET = object()

APPROVAL_MODES = ("ask", "auto", "full")


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
