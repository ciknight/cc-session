"""对已安装 cc-switch CLI 的受限调用，仅向调用方暴露安全摘要。"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from session_preferences import validate_model


class SwitchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ABSENT = "absent"


def _log_failure(stage, *, returncode=None, error=None, stderr=None):
    """仅记录白名单摘要；CLI 输出和异常原文可能含完整配置与凭据。"""
    fields = [f"[cc-switch] {stage}"]
    if returncode is not None:
        fields.append(f"退出码={returncode}")
    if error is not None:
        fields.append(f"异常类型={type(error).__name__}")
        for key in ("errno", "winerror"):
            value = getattr(error, key, None)
            if isinstance(value, int):
                fields.append(f"{key}={value}")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if isinstance(stderr, str):
        text = stderr.lower()
        # 只将已知错误映射为固定提示，绝不复制原始匹配内容。
        reasons = (
            (("is not recognized as an internal or external command", "不是内部或外部命令"), "命令无法识别"),
            (("enoent", "no such file or directory"), "文件或命令不存在"),
            (("eacces", "eperm", "permission denied", "access is denied"), "访问被拒绝"),
            (("syntaxerror", "invalid json", "unexpected token"), "配置或脚本解析失败"),
        )
        reason = next((hint for patterns, hint in reasons if any(p in text for p in patterns)),
                      "未识别的 CLI 错误（原始输出已隐藏）")
        fields.append(f"原因={reason}")
    try:
        print("；".join(fields), flush=True)
    except (OSError, ValueError):
        pass  # 诊断输出失败不得覆盖原来的切换错误。


class CCSwitch:
    def __init__(self, claude_dir, home=None, command="cc-switch", timeout=30):
        self.claude_dir = Path(claude_dir).expanduser().resolve()
        self.home = Path(home or Path.home()).expanduser().resolve()
        self.profile_dir = self.home / ".cc-switch" / "profiles"
        self.command = command
        self.timeout = timeout

    def _target(self, cwd):
        if not isinstance(cwd, str) or not cwd:
            raise SwitchError(400, "必须指定项目目录")
        try:
            project = Path(cwd).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SwitchError(400, "项目路径无效或不可访问") from exc
        if not project.is_dir():
            raise SwitchError(400, "项目路径不是目录")
        config_dir = project / ".claude"
        settings = config_dir / "settings.local.json"
        # 不允许配置目录或目标文件将写入重定向到项目之外。
        if config_dir.is_symlink() or settings.is_symlink():
            raise SwitchError(400, "目标配置路径不能是符号链接")
        if settings.exists() and not settings.is_file():
            raise SwitchError(400, "目标配置必须是普通文件")
        return project, settings

    def _revision(self, target):
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            return _ABSENT
        except OSError as exc:
            raise SwitchError(500, "无法读取目标配置文件") from exc
        return hashlib.sha256(data).hexdigest()

    def _binary(self):
        # 允许管理员显式配置安装命令；默认只查询 PATH，不寻找邻仓代码。
        candidate = self.command
        if candidate == "cc-switch":
            candidate = shutil.which("cc-switch.ps1") or shutil.which("cc-switch")
        else:
            candidate = shutil.which(candidate) if not Path(candidate).is_absolute() else candidate
        return candidate

    def _command_argv(self, binary, profile):
        path = Path(binary)
        suffix = path.suffix.lower()
        if os.name == "nt" and suffix == ".ps1":
            shell = shutil.which("pwsh") or shutil.which("powershell")
            if not shell:
                raise SwitchError(503, "找到 cc-switch，但系统中没有 PowerShell 执行器")
            return [shell, "-NoProfile", "-NonInteractive", "-File", str(path), "local", profile]
        if os.name == "nt" and suffix == ".cmd":
            if any(c in str(path) for c in '&|<>^()%!"'):
                raise SwitchError(503, "cc-switch 安装路径含不安全的命令行字符，请使用 PowerShell shim")
            shell = os.environ.get("COMSPEC", "cmd.exe")
            # 该 .cmd 是 PATH 中解析出的已安装命令，profile 已限于安全文件名。
            # CMD 命令体必须保留双层引号，不能让 subprocess 再按 argv 转义。
            prefix = subprocess.list2cmdline([shell, "/d", "/s", "/c"])
            return f'{prefix} ""{path}" local {profile}"'
        return [str(path), "local", profile]

    def status(self, cwd):
        project, target = self._target(cwd)
        binary = self._binary()
        profiles = []
        model_options = []
        try:
            if self.profile_dir.is_dir():
                profile_files = sorted(
                    (p for p in self.profile_dir.glob("*.json")
                     if p.is_file() and not p.is_symlink() and _NAME.fullmatch(p.stem)),
                    key=lambda p: p.stem,
                )
                if binary:
                    profiles = [p.stem for p in profile_files]
        except OSError:
            profiles = []
        available = self.claude_dir == (self.home / ".claude").resolve()
        reason = None
        if not binary:
            reason = "未安装 cc-switch，无法切换项目配置"
        elif not available:
            reason = "cc-switch 使用默认 ~/.claude；当前 CLAUDE_CONFIG_DIR 不同，已禁用配置切换"
        elif not profiles:
            reason = "cc-switch 未找到可用的 profile"
        current = None
        if target.exists():
            try:
                value = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    if isinstance(value.get("_ccSwitchProfile"), str):
                        current = value["_ccSwitchProfile"]
                    env = value.get("env")
                    if isinstance(env, dict):
                        # 仅取当前目录 local 配置，不读取其他 profile 或全局配置的模型。
                        for family in ("Opus", "Sonnet", "Haiku", "Fable"):
                            key = f"{family.upper()}_MODEL"
                            model = env.get(key) if key in env else env.get(f"ANTHROPIC_DEFAULT_{key}")
                            try:
                                model = validate_model(model)
                            except ValueError:
                                continue
                            if model is not None:
                                model_options.append({"name": f"{family}: {model}", "model": model})
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        return {"installed": bool(binary), "available": bool(binary and available and profiles),
                "reason": reason, "profiles": profiles, "model_options": model_options,
                "current_profile": current, "target_cwd": str(project), "target_path": str(target),
                "revision": self._revision(target)}

    def operation(self, cwd):
        """串行化同一项目的配置切换和调用方保护的进程启动。"""
        if not isinstance(cwd, str) or not cwd:
            raise SwitchError(400, "必须指定项目目录")
        try:
            project = Path(cwd).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SwitchError(400, "项目路径无效或不可访问") from exc
        if not project.is_dir():
            raise SwitchError(400, "项目路径不是目录")
        key = os.path.normcase(str(project))
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.RLock())
        return lock

    def apply(self, cwd, profile, revision, confirm=False):
        if not confirm:
            raise SwitchError(400, "请先确认目标配置将被整体覆盖")
        if not isinstance(profile, str) or not _NAME.fullmatch(profile):
            raise SwitchError(400, "profile 名称无效")
        project, target = self._target(cwd)
        lock = self.operation(str(project))
        with lock:
            state = self.status(str(project))
            if not state["installed"]:
                raise SwitchError(503, state["reason"])
            if not state["available"]:
                raise SwitchError(400, state["reason"] or "项目配置不可用")
            if profile not in state["profiles"]:
                raise SwitchError(400, "所选 profile 不存在或不可用")
            if not isinstance(revision, str) or revision != state["revision"]:
                raise SwitchError(409, "目标配置已变化，请刷新后重新确认")
            binary = self._binary()
            if not binary:
                raise SwitchError(503, "未安装 cc-switch，无法切换项目配置")
            backup_path = None
            backed_up = target.exists()
            if backed_up:
                try:
                    original = target.read_bytes()
                    backup_dir = self.claude_dir / "claude-sessions" / "backups" / hashlib.sha256(
                        str(target).encode("utf-8", "surrogatepass")).hexdigest()[:16]
                    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd, name = tempfile.mkstemp(prefix=time.strftime("%Y%m%d-%H%M%S-"),
                                                suffix=".json", dir=backup_dir)
                    backup_path = Path(name)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(original)
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as exc:
                    raise SwitchError(500, "备份原配置失败，未调用 cc-switch") from exc
            try:
                proc = subprocess.run(self._command_argv(binary, profile), cwd=str(project),
                                      stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout,
                                      text=True, encoding="utf-8", errors="replace", check=False)
            except subprocess.TimeoutExpired as exc:
                _log_failure("执行超时", error=exc)
                raise SwitchError(504, "cc-switch 执行超时；目标配置可能已改变，请刷新核实") from exc
            except OSError as exc:
                _log_failure("无法启动命令", error=exc)
                raise SwitchError(503, "无法运行已安装的 cc-switch 命令") from exc
            if proc.returncode:
                _log_failure("CLI 执行失败", returncode=proc.returncode, stderr=proc.stderr)
                raise SwitchError(500, f"cc-switch 切换失败（退出码 {proc.returncode}，详见服务端日志）；目标配置可能已改变，请刷新核实")
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                _log_failure("目标配置无法读取或解析", error=exc)
                raise SwitchError(500, "cc-switch 已退出，但目标配置无法验证；请刷新核实") from exc
            if not isinstance(data, dict) or data.get("_ccSwitchProfile") != profile:
                _log_failure("目标 profile 不匹配")
                raise SwitchError(500, "cc-switch 已退出，但目标 profile 与请求不一致；请刷新核实")
            result = self.status(str(project))
            result.update(backed_up=backed_up, backup_path=str(backup_path) if backup_path else None,
                          message="local 配置已写入；已有托管进程未重启，请退出后重新托管")
            return result
