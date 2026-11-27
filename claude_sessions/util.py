"""无状态工具函数。

now_ts 是测试的时钟替换点：各模块以 util.now_ts() 形式调用，
patch util.now_ts 即可全局生效；其余纯函数可自由 from import。
"""
import base64
import binascii
import json
import os
import platform
import re
import shutil
import time
from pathlib import Path

from . import config


def now_ts():
    return time.time()


def clip(s, n=300):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def is_windows_batch_path(path):
    """路径任一组件是否以 .bat/.cmd 结尾（Windows 会经 cmd.exe 重新解析参数）。"""
    if not path:
        return False
    for component in str(path).replace("\\", "/").split("/"):
        for segment in component.split(":"):
            if segment.rstrip(". ").lower().endswith((".bat", ".cmd")):
                return True
    return False


def is_windows_native_exe(path):
    """路径最后一段是否为 CreateProcess 可直接执行的 .exe/.com。"""
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name.rstrip(". ").lower().endswith((".exe", ".com"))


def resolve_claude_bin(raw, which=None, platform_name=None, home=None):
    """解析 claude 可执行文件；Windows 上优先原生 exe，绕开 npm 的 .cmd 包装。

    .cmd/.bat 只能由 cmd.exe 执行，命令行会被二次解析（BatBadBut/CVE-2024-27980），
    且超时终止只杀到 cmd.exe。只对默认的裸命令名/批处理 shim 自动替换；显式路径
    和自定义命令名保持用户选择（调用方负责告警），避免绕过 wrapper 的环境设置。
    候选顺序：shim 同目录的原生 exe → PATH 里的 claude.exe → 原生安装器。
    """
    which = shutil.which if which is None else which
    platform_name = platform.system() if platform_name is None else platform_name
    home = Path.home() if home is None else Path(home)
    raw_text = str(raw)
    path = which(raw) or raw
    if platform_name != "Windows" or is_windows_native_exe(path):
        return path

    explicit = "/" in raw_text or "\\" in raw_text
    name = Path(raw_text).name.lower()
    if explicit or name not in ("claude", "claude.cmd", "claude.bat", "claude.ps1"):
        return path

    prefix = Path(path).parent
    candidates = [
        str(prefix / "node_modules" / "@anthropic-ai"
            / "claude-code" / "bin" / "claude.exe"),
        which("claude.exe"),
        str(home / ".local" / "bin" / "claude.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return path


# 从 Claude Code 会话里启动 monitor 时，这些变量描述的是父会话/父进程，
# 不能带进被托管的 claude 子进程（否则子进程会误连父会话的消息 socket）。
CHILD_ENV_DROP = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
    "AI_AGENT",
)


def child_env(cwd=None, base=None):
    """为 claude 子进程准备环境变量：只去掉 Claude Code 的会话内部变量。"""
    env = dict(os.environ if base is None else base)
    for key in CHILD_ENV_DROP:
        env.pop(key, None)
    if cwd:
        env["PWD"] = str(cwd)
    return env


def result_error_text(data, fallback=""):
    """把 result 的结构化错误字段拼成可读文本（result/errors/HTTP 状态/终止原因）。"""
    data = data if isinstance(data, dict) else {}
    parts = []
    result = data.get("result")
    if isinstance(result, str) and result.strip():
        parts.append(result.strip())
    errors = data.get("errors")
    if isinstance(errors, str):
        errors = [errors]
    if isinstance(errors, list):
        parts.extend(e.strip() for e in errors if isinstance(e, str) and e.strip())
    status = data.get("api_error_status")
    if isinstance(status, int):
        parts.append(f"HTTP {status}")
    reason = data.get("terminal_reason")
    if isinstance(reason, str) and reason.strip() \
            and reason.strip().lower() not in ("completed", "success"):
        parts.append(reason.strip())
    if not parts:
        subtype = data.get("subtype")
        if isinstance(subtype, str) and subtype.strip() \
                and subtype.strip().lower() != "success":
            parts.append(subtype.strip())
        elif fallback:
            parts.append(str(fallback).strip())
        else:
            parts.append("error")
    return clip(" | ".join(dict.fromkeys(p for p in parts if p)), 500)


def active_rate_limit(record, now=None):
    """返回未过期的限流记录；resets_at 已过或记录无效时返回 None。"""
    if not isinstance(record, dict):
        return None
    resets_at = record.get("resets_at")
    now = now_ts() if now is None else now
    if isinstance(resets_at, (int, float)) and 0 < resets_at <= now:
        return None
    return record


_NETWORK_CODES = {
    "ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED", "ECONNRESET", "ECONNABORTED",
    "ETIMEDOUT", "EPIPE", "EHOSTUNREACH", "ENETUNREACH", "UND_ERR_CONNECT_TIMEOUT",
    "CERT_HAS_EXPIRED", "UNABLE_TO_VERIFY_LEAF_SIGNATURE", "ERR_TLS_CERT_ALTNAME_INVALID",
}
_NETWORK_MARKERS = (
    "connection error", "connection dropped", "network", "getaddrinfo",
    "socket hang up", "tls", "ssl", "fetch failed", "econnreset", "enotfound",
    "econnrefused", "etimedout", "eai_again", "http/2 cancel",
)
_AUTH_MARKERS = ("unauthorized", "authentication", "invalid api key",
                 "permission denied", "forbidden", "401", "403")
_RATE_MARKERS = ("rate limit", "rate_limit", "overloaded", "too many requests",
                 "quota", "insufficient_quota", "no available upstream")


def classify_api_error(text, status=None, connection=None, network_down=False):
    """把上游错误归成 network / rate_limit / server / auth / other。

    转录里的错误文本形态很杂（连接失败、网关 5xx、529 过载、鉴权失败），
    分类只用于网页选择更准确的标题，具体原因仍原样展示 text。
    """
    low = (text or "").lower()
    if status in (401, 403) or any(m in low for m in _AUTH_MARKERS):
        return "auth"
    if network_down:
        return "network"
    code = connection.get("code") if isinstance(connection, dict) else None
    if isinstance(code, str) and code.upper() in _NETWORK_CODES:
        return "network"
    if any(m in low for m in _NETWORK_MARKERS):
        return "network"
    if status == 429 or "529" in low or any(m in low for m in _RATE_MARKERS):
        return "rate_limit"
    if (isinstance(status, int) and status >= 500) or any(
            m in low for m in ("server_error", "internal server error", "500", "503")):
        return "server"
    return "other"


def api_error_fields(obj):
    """从 system/api_error、system/api_retry 或 assistant isApiErrorMessage 行
    提取看板需要的结构化错误字段。

    文本优先取 error.formatted（CLI 会给出更具体的说明，例如 DNS/网关提示），
    而不是只有 "Connection error." 的 error.message。
    """
    obj = obj if isinstance(obj, dict) else {}
    err = obj.get("error")
    if isinstance(err, dict):
        text = err.get("formatted") or err.get("message") or ""
        status = err.get("status")
        connection = err.get("connection")
        network_down = bool(err.get("isNetworkDown"))
    else:
        text = err if isinstance(err, str) else ""
        status = None
        connection = None
        network_down = False
    if not text and isinstance(obj.get("message"), str):
        text = obj["message"]
    if status is None:
        status = obj.get("error_status")
    if status is None:
        status = obj.get("api_error_status")
    attempt = obj.get("retryAttempt", obj.get("attempt"))
    max_retries = obj.get("maxRetries", obj.get("max_retries"))
    retry_in_ms = obj.get("retryInMs", obj.get("retry_in_ms"))
    kind = classify_api_error(text, status, connection, network_down)
    fields = {"text": clip(text or "模型服务暂时不可用", 300), "kind": kind}
    for key, value in (("status", status), ("retry_attempt", attempt),
                       ("max_retries", max_retries), ("retry_in_ms", retry_in_ms)):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[key] = int(value)
    code = connection.get("code") if isinstance(connection, dict) else None
    if isinstance(code, str) and code:
        fields["code"] = code
    source = obj.get("source")
    if isinstance(source, str) and source:
        fields["source"] = source
    return fields


def message_content(text, images=None):
    """把网页消息整理成 CLI stream-json 的文本/图片 content blocks。"""
    if not isinstance(text, str):
        raise ValueError('body must contain string "text"')
    images = [] if images is None else images
    if not isinstance(images, list):
        raise ValueError("images 必须是数组")
    if len(images) > config.IMAGE_MAX_COUNT:
        raise ValueError(f"一次最多发送 {config.IMAGE_MAX_COUNT} 张图片")
    if not text.strip() and not images:
        raise ValueError("消息不能为空")
    blocks = [{"type": "text", "text": text}] if text else []
    total = 0
    for index, image in enumerate(images, 1):
        if not isinstance(image, dict):
            raise ValueError(f"第 {index} 张图片格式无效")
        media_type = image.get("media_type")
        data = image.get("data")
        if not isinstance(media_type, str) or media_type not in config.IMAGE_MEDIA_TYPES \
                or not isinstance(data, str):
            raise ValueError(f"第 {index} 张图片仅支持 PNG / JPEG / GIF / WebP base64 数据")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"第 {index} 张图片的 base64 数据无效") from e
        if not raw or len(raw) > config.IMAGE_MAX_BYTES:
            raise ValueError(f"第 {index} 张图片不能为空且不能超过 3.75 MiB")
        total += len(raw)
        if total > config.IMAGE_TOTAL_BYTES:
            raise ValueError("一次发送的图片总大小不能超过 20 MiB")
        blocks.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": data}})
    return blocks if blocks and any(b["type"] == "image" for b in blocks) else text


def message_summary(content):
    """任务/队列只保留文字摘要，绝不把图片 base64 放进 job 或事件。"""
    if isinstance(content, str):
        return content
    texts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    count = sum(isinstance(b, dict) and b.get("type") == "image" for b in content)
    summary = " ".join(texts).strip()
    if count:
        summary += (" " if summary else "") + f"[图片 {count} 张]"
    return summary


def _arg_present(args, name):
    return any(arg == name or arg.startswith(name + "=") for arg in args)


def _arg_index(args, name):
    for i, arg in enumerate(args):
        if arg == name:
            return i, (args[i + 1] if i + 1 < len(args) else None)
        if arg.startswith(name + "="):
            return i, arg.split("=", 1)[1]
    return None, None


def _merge_hook_settings(existing, hook_settings):
    """把 AskUserQuestion hook 合并进已有的内联 settings；无法合并时返回 None。"""
    hooks = existing.get("hooks")
    if hooks is None:
        hooks = existing["hooks"] = {}
    if not isinstance(hooks, dict):
        return None
    pre = hooks.get("PreToolUse")
    if pre is None:
        pre = hooks["PreToolUse"] = []
    if not isinstance(pre, list):
        return None
    for rule in pre:
        if not isinstance(rule, dict) or rule.get("matcher") != "AskUserQuestion":
            continue
        for hook in rule.get("hooks") or []:
            if isinstance(hook, dict) and "monitor.py" in str(hook.get("command", "")):
                return existing
    pre.extend(hook_settings["hooks"]["PreToolUse"])
    return existing


def project_root(cwd):
    """把 worktree 目录归并到主项目：<proj>/.claude/worktrees/<name> -> <proj>"""
    if not cwd:
        return cwd
    norm = cwd.replace("\\", "/")
    for marker in ("/.claude/worktrees/", "/.worktrees/"):
        idx = norm.find(marker)
        if idx > 0:
            return cwd[:idx]
    return cwd


def path_is_within(path, root):
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except ValueError:
        return False


def _parse_iso(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text):
    return _ANSI.sub("", text or "")
