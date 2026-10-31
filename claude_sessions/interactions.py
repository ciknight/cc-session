"""权限 / 计划 / 问题交互：等待用户回答的一次性请求及其预览生成。"""
import difflib
import json
import threading

from . import util
from .util import clip, strip_ansi


def permission_preview(tool_name, tool_input):
    """为审批卡片生成 diff / 命令 / 计划 / JSON 预览。"""
    ti = tool_input or {}
    if tool_name == "ExitPlanMode":
        plan = ti.get("plan")
        text = plan if isinstance(plan, str) else json.dumps(ti, ensure_ascii=False)
        return {"kind": "text", "text": clip(text, 8000)}
    if tool_name == "Write":
        path = ti.get("file_path") or ti.get("path") or "new file"
        diff = "\n".join(difflib.unified_diff(
            [], (ti.get("content") or "").splitlines(),
            fromfile="/dev/null", tofile=path, lineterm=""))
        return {"kind": "diff", "text": clip(diff, 8000)}
    if "old_string" in ti and "new_string" in ti:
        path = ti.get("file_path") or ti.get("path") or ""
        diff = "\n".join(difflib.unified_diff(
            str(ti.get("old_string", "")).splitlines(),
            str(ti.get("new_string", "")).splitlines(),
            fromfile=path, tofile=path, lineterm=""))
        return {"kind": "diff", "text": clip(diff, 8000)}
    if "command" in ti:
        return {"kind": "text", "text": clip(ti.get("command", ""), 4000)}
    return {"kind": "text", "text": clip(json.dumps(ti, ensure_ascii=False), 4000)}


class Interaction:
    """一次等待用户回答的权限 / 计划 / 问题交互。"""

    def __init__(self, request_id, hosted, request, session_id=None,
                 kind=None, questions=None):
        self.id = request_id
        self.hosted_id = getattr(hosted, "hid", None)
        self.session_id = session_id or getattr(hosted, "session_id", None)
        self.request = request
        self.tool_name = request.get("tool_name") or ""
        self.tool_use_id = request.get("tool_use_id")
        self.tool_input = request.get("input") or {}
        self.reason = strip_ansi(request.get("decision_reason") or "")
        self.suggestions = [s for s in (request.get("permission_suggestions") or [])
                            if isinstance(s, dict) and s.get("behavior") == "allow"]
        self.allow_always = bool(self.suggestions) and not request.get("suppress_always_allow_rule")
        self.preview = permission_preview(self.tool_name, self.tool_input)
        self.kind = kind or ("plan" if self.tool_name == "ExitPlanMode" else "permission")
        self.questions = questions or []
        self.answers = None
        self.created_at = util.now_ts()
        self.event = threading.Event()
        self.behavior = None
        self.scope = "once"
        self.message = None
        self.updated_input = self.tool_input
        self.state = "pending"
        self.resolved_at = None
        self.skip_response = False   # CLI 已取消 / 进程已退出时不再回写
        self.lock = threading.Lock()  # resolve 的检查与置位原子化

    def resolve(self, behavior, scope="once", reason=None, state=None):
        with self.lock:
            if self.state != "pending":
                return False
            self.behavior = behavior
            self.scope = scope
            self.message = reason
            self.state = state or ("allowed" if behavior == "allow" else "denied")
            self.resolved_at = util.now_ts()
            self.event.set()
            return True

    def public(self):
        return {"id": self.id, "hosted_id": self.hosted_id, "session_id": self.session_id,
                "tool_name": self.tool_name, "tool_use_id": self.tool_use_id,
                "tool_input": self.tool_input, "preview": self.preview,
                "reason": self.reason, "allow_always": self.allow_always,
                "kind": self.kind, "questions": self.questions,
                "created_at": self.created_at, "state": self.state,
                "behavior": self.behavior, "scope": self.scope,
                "resolved_at": self.resolved_at}
