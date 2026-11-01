"""无状态工具函数。

now_ts 是测试的时钟替换点：各模块以 util.now_ts() 形式调用，
patch util.now_ts 即可全局生效；其余纯函数可自由 from import。
"""
import base64
import binascii
import json
import os
import re
import time

from . import config


def now_ts():
    return time.time()


def clip(s, n=300):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


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
