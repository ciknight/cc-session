"""无状态工具函数。

now_ts 是测试的时钟替换点：各模块以 util.now_ts() 形式调用，
patch util.now_ts 即可全局生效；其余纯函数可自由 from import。
"""
import json
import os
import re
import time


def now_ts():
    return time.time()


def clip(s, n=300):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


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
