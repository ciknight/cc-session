"""Session：单个转录文件的累计解析状态，以及列表/详情的对外投影。"""
import copy
from collections import Counter, deque

from . import util
from .util import project_root


class Session:
    def __init__(self, sid, path):
        self.id = sid
        self.path = path
        self.cwd = None
        self.title = None            # 最终展示标题（custom-title 优先）
        self.ai_title = None
        self.custom_title = None
        self.model = None
        self.version = None          # Claude Code 版本
        self.git_branch = None
        self.offset = 0
        self.buf = b""
        self.tasks = {}              # taskId -> {id, subject, status}
        self.task_seq = 0
        self.pending_create = {}     # tool_use_id -> subject
        self.subagents = []          # Agent 工具调用: {type, description, ts, tid, state}
        self.active_agents = set()    # 尚未收到匹配 tool_result 的 Agent tool_use id
        self.tool_uses = Counter()   # 工具名 -> 次数
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        self.user_msgs = 0
        self.assistant_msgs = 0
        self.first_ts = None
        self.first_user = None
        self.last_user = None
        self.last_assistant = None
        self.last_event_ts = 0
        self.last_input_ts = None      # 只由真实用户输入推进
        self.last_turn_end_ts = None   # system/turn_duration
        self.saw_turn_duration = False
        self.pending_tools = set()     # 主链上尚未回传结果/未结束的 tool_use id
        self.context_tokens = 0        # 最新一条 assistant usage 的上下文大小
        self.compacted_at = None
        self.last_api_error = None     # 最近一次上游 API 错误/重试（system/api_error）
        self.is_subagent = False
        self.parent_id = None
        self.agent_name = None
        self.state = "idle"
        self.recent = deque(maxlen=50)
        self.tail_sample = b""        # 文件末尾 4 KiB，用于校验扫描缓存没有过期

    def cache_state(self):
        """返回可 JSON 序列化的累计状态；offset/buf 由调用方单独保存。"""
        return {
            "cwd": self.cwd, "title": self.title, "ai_title": self.ai_title,
            "custom_title": self.custom_title, "model": self.model,
            "version": self.version, "git_branch": self.git_branch,
            "tasks": copy.deepcopy(self.tasks), "task_seq": self.task_seq,
            "pending_create": dict(self.pending_create),
            "subagents": copy.deepcopy(self.subagents),
            "active_agents": sorted(self.active_agents),
            "tool_uses": dict(self.tool_uses),
            "tokens": dict(self.tokens),
            "user_msgs": self.user_msgs, "assistant_msgs": self.assistant_msgs,
            "first_ts": self.first_ts, "first_user": self.first_user,
            "last_user": self.last_user, "last_assistant": self.last_assistant,
            "last_event_ts": self.last_event_ts, "last_input_ts": self.last_input_ts,
            "last_turn_end_ts": self.last_turn_end_ts,
            "saw_turn_duration": self.saw_turn_duration,
            "pending_tools": sorted(self.pending_tools),
            "context_tokens": self.context_tokens,
            "compacted_at": self.compacted_at,
            "last_api_error": copy.deepcopy(self.last_api_error),
            "agent_name": self.agent_name,
            "state": self.state,
        }

    def restore_cache(self, data):
        """从扫描缓存恢复累计状态；只做宽松校验，坏字段回退默认值。"""
        if not isinstance(data, dict):
            return
        for key in ("cwd", "title", "ai_title", "custom_title", "model",
                    "version", "git_branch", "first_user", "last_user",
                    "last_assistant", "agent_name"):
            value = data.get(key)
            if isinstance(value, str) or value is None:
                setattr(self, key, value)
        for key in ("first_ts", "last_event_ts", "last_input_ts",
                    "last_turn_end_ts", "compacted_at"):
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, (int, float)):
                setattr(self, key, value)
            elif value is None and key != "last_event_ts":
                setattr(self, key, None)
        for key in ("user_msgs", "assistant_msgs", "task_seq", "context_tokens"):
            value = data.get(key)
            if isinstance(value, int) and value >= 0:
                setattr(self, key, value)
        self.saw_turn_duration = bool(data.get("saw_turn_duration"))
        state = data.get("state")
        if state in ("working", "idle"):
            self.state = state
        tasks = data.get("tasks")
        if isinstance(tasks, dict):
            self.tasks = {str(k): v for k, v in tasks.items() if isinstance(v, dict)}
        pending_create = data.get("pending_create")
        if isinstance(pending_create, dict):
            self.pending_create = dict(pending_create)
        subagents = data.get("subagents")
        if isinstance(subagents, list):
            self.subagents = [x for x in subagents if isinstance(x, dict)]
            # 旧缓存条目没有 tid/state：无法继续配对，一律视为已完成
            for sub in self.subagents:
                if not isinstance(sub.get("state"), str):
                    sub["state"] = "done"
                if not isinstance(sub.get("tid"), str):
                    sub["tid"] = ""
        self.active_agents = {x for x in (data.get("active_agents") or []) if isinstance(x, str)}
        self.pending_tools = {x for x in (data.get("pending_tools") or []) if isinstance(x, str)}
        tool_uses = data.get("tool_uses")
        if isinstance(tool_uses, dict):
            self.tool_uses = Counter({str(k): v for k, v in tool_uses.items()
                                      if isinstance(v, int)})
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            self.tokens = {k: tokens.get(k, 0) if isinstance(tokens.get(k, 0), int) else 0
                           for k in ("input", "output", "cache_read", "cache_creation")}
        error = data.get("last_api_error")
        self.last_api_error = error if isinstance(error, dict) else None

    def _is_stale(self, stale_after):
        """超过 stale 秒无内容事件。父进程异常退出留下的截断转录没有回合结束
        标记，靠它把残留的运行中子代理在输出层降级。"""
        return bool(self.last_event_ts and util.now_ts() - self.last_event_ts > stale_after)

    def summary(self, stale_after, project=None):
        state = self.state
        stale = self._is_stale(stale_after)
        if stale:
            state = "stale"
        elif self.active_agents:
            state = "working"
        done = sum(1 for t in self.tasks.values() if t["status"] == "completed")
        return {
            "id": self.id,
            "cwd": self.cwd or self.path.parent.name,
            "project": project or project_root(self.cwd) or self.path.parent.name,
            "title": self.title,
            "model": self.model,
            "version": self.version,
            "git_branch": self.git_branch,
            "state": state,
            "first_event_at": self.first_ts,
            "last_event_at": self.last_event_ts,
            "last_input_at": self.last_input_ts,
            "last_turn_end_at": self.last_turn_end_ts,
            "context_tokens": self.context_tokens,
            "compacted_at": self.compacted_at,
            "last_api_error": self.last_api_error,
            "is_subagent": self.is_subagent,
            "parent_id": self.parent_id,
            "agent_name": self.agent_name,
            "last_user_message": self.last_user,
            "first_user_message": self.first_user,
            "last_assistant_message": self.last_assistant,
            "tasks_total": len(self.tasks),
            "tasks_completed": done,
            "subagent_count": len(self.subagents),
            # 会话停滞时同步子代理是截断转录的残留，不计入运行中；
            # 异步子代理无回合寿命，仍可能在后台运行
            "subagents_running": sum(1 for a in self.subagents
                                     if a.get("state") == "running"
                                     and (not stale or a.get("async"))),
            "user_msgs": self.user_msgs,
            "assistant_msgs": self.assistant_msgs,
            "tokens": dict(self.tokens),
        }

    def detail(self, stale_after, project=None):
        d = self.summary(stale_after, project)
        d["tasks"] = list(self.tasks.values())
        # 会话停滞时同步运行条目输出为 stale（副本，累计状态不动）；
        # 活跃会话一旦恢复，条目自动回到 running
        subs = self.subagents
        if self._is_stale(stale_after):
            subs = [dict(sub, state="stale")
                    if sub.get("state") == "running" and not sub.get("async") else sub
                    for sub in subs]
        # 运行中的子代理排最前，其余按 ts 倒序
        d["subagents"] = sorted(subs,
                                key=lambda sub: (sub.get("state") != "running",
                                                 -sub.get("ts", 0)))
        d["tool_uses"] = dict(self.tool_uses.most_common())
        d["recent_events"] = list(self.recent)
        return d
