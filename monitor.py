#!/usr/bin/env python3
"""
monitor.py — 监听 Claude Code 会话的任务内容和状态

数据来源:
  ~/.claude/projects/<项目>/<sessionId>.jsonl   会话转录，逐行追加
  ~/.claude/tasks/<sessionId>/*.json            任务清单（如果存在，作为补充）

接口 (默认 http://127.0.0.1:8765):
  GET /                       网页看板：左侧项目列表，右侧会话列表/对话视图，自动刷新可开关
  GET /sessions               所有会话概览（?full=1 时含任务、子代理、最近事件等完整信息）
  GET /sessions/<id>          按 id 取单个会话完整信息
  GET /sessions/<id>/messages 按需解析该会话的完整对话（带缓存，跳过 sidechain 子代理对话）
  GET /static/<file>          前端静态资源（marked / DOMPurify）
  GET /events                 SSE 实时事件流（?session=<id> 可过滤）
  GET /healthz                健康检查

远程发消息:
  POST /sessions/<id>/message  给会话发消息 {"text": "...", "force": false}，返回 job_id。
                               内部执行 `claude -p --resume <id>`（工作目录取会话的 cwd，
                               环境变量原样继承，所以自建 API / ANTHROPIC_BASE_URL 同样适用）
  GET /jobs/<job_id>           查询发消息任务的状态和回复 (queued / running / done / failed)
  POST /new                    {"cwd": "<目录>", "text": "首条消息"} 在该目录跑一轮全新会话
                               （不托管，每条消息重启一次 claude）；job 完成后
                               result_session_id 即新会话 id

托管会话（常驻 claude 进程，后续消息直接写 stdin，不再每次重启）:
  POST /hosted                 {"resume_id": "<已有会话id>" 或 "cwd": "<新建会话的目录>",
                               "text": "第一条消息", "force": false}
                               之后给该会话 POST /sessions/<id>/message 会自动走常驻进程，
                               模型忙时消息排队，一轮结束再发下一条
  GET /hosted                  托管进程列表（状态、是否忙、排队数、轮数）
  DELETE /hosted/<hid>         正常退出（做完当前一轮）；加 ?kill=1 立即终止
  注意: 要托管已有会话，先在终端里把它退出，避免两个进程同时写一个会话。

接口无鉴权，发消息/托管可远程执行 claude，请只在可信网络内暴露。

每个会话可展示的信息（均解析自转录文件）:
  标题(ai-title)、项目目录(cwd)、git 分支、模型、Claude 版本、
  首/末事件时间(时长)、用户/助手消息数、token 用量(input/output/cache)、
  工具调用统计、子代理(Agent 工具)列表、任务清单(TaskCreate/TaskUpdate 或 tasks 目录)

会话状态:
  working  最近有活动，且最后一个事件不是"助手完成回复"
  idle     助手已完成回复（stop_reason=end_turn），等待用户输入
  stale    超过 --stale 秒没有任何更新

用法:
  python monitor.py                      # 监听默认目录
  python monitor.py --port 9000 --stale 600
  python monitor.py --webhook http://localhost:3000/hook   # 事件同时 POST 出去
"""
import argparse
import difflib
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape
from session_preferences import (APPROVAL_MODES, ApprovalPreferences, ModelPreferences,
                                 UNSET, default_model, model_args, validate_model)
from cc_switch import CCSwitch, SwitchError

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

_jinja = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def now_ts():
    return time.time()


def clip(s, n=300):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def _arg_present(args, name):
    return any(arg == name or arg.startswith(name + "=") for arg in args)


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
        self.subagents = []          # Agent 工具调用: {type, description, ts}
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
        self.is_subagent = False
        self.parent_id = None
        self.agent_name = None
        self.state = "idle"
        self.recent = deque(maxlen=50)

    def summary(self, stale_after, project=None):
        state = self.state
        if self.last_event_ts and now_ts() - self.last_event_ts > stale_after:
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
            "is_subagent": self.is_subagent,
            "parent_id": self.parent_id,
            "agent_name": self.agent_name,
            "last_user_message": self.last_user,
            "first_user_message": self.first_user,
            "last_assistant_message": self.last_assistant,
            "tasks_total": len(self.tasks),
            "tasks_completed": done,
            "subagent_count": len(self.subagents),
            "user_msgs": self.user_msgs,
            "assistant_msgs": self.assistant_msgs,
            "tokens": dict(self.tokens),
        }

    def detail(self, stale_after, project=None):
        d = self.summary(stale_after, project)
        d["tasks"] = list(self.tasks.values())
        d["subagents"] = sorted(self.subagents, key=lambda sub: sub.get("ts", 0), reverse=True)
        d["tool_uses"] = dict(self.tool_uses.most_common())
        d["recent_events"] = list(self.recent)
        return d


class Monitor:
    def __init__(self, stale_after, webhook=None, permission_timeout=300):
        # 以文件路径为键：同一 session id 可能出现在多个项目目录（恢复/移动过），
        # 若按 id 键控，两个文件的读取 offset 会互相干扰导致重复计数
        self.sessions = {}           # str(path) -> Session
        self.stale_after = stale_after
        self.webhook = webhook
        self.permission_timeout = permission_timeout
        self.interactions = {}       # request_id -> Interaction
        self.lock = threading.Lock()
        self.subscribers = []      # list of queue.Queue
        self.first_scan_done = False
        self.msg_cache = {}        # sid -> {off, buf, msgs, body} 对话接口的增量缓存
        # 远程发消息 / 托管相关
        self.claude_bin = "claude"
        self.claude_args = []
        self.msg_timeout = 600
        self.jobs = {}             # job_id -> dict
        self.busy = set()          # 正在执行远程消息的 session id
        self.new_cwd_busy = set()  # 尚未取得 session id 的新会话目录
        self.hosted = {}           # hosted_id -> Hosted（服务托管的常驻进程）
        self.hosted_by_sid = {}    # session id -> Hosted
        self.preferences = ModelPreferences(CLAUDE_DIR / "claude-sessions" / "preferences.json")
        self.approvals = ApprovalPreferences(CLAUDE_DIR / "claude-sessions" / "approvals.json")
        self.cc_switch = CCSwitch(CLAUDE_DIR)

    def by_sid(self, sid):
        """按 session id 查找；同名取最近活跃的一个。"""
        best = None
        for s in self.sessions.values():
            if s.id == sid and (best is None or s.last_event_ts > best.last_event_ts):
                best = s
        return best

    def unique_sessions(self, include_subagents=False):
        """按 session id 去重后的会话列表（同名取最近活跃的）。"""
        best = {}
        for s in self.sessions.values():
            cur = best.get(s.id)
            if cur is None or s.last_event_ts > cur.last_event_ts:
                best[s.id] = s
        return [s for s in best.values() if include_subagents or not s.is_subagent]

    def project_map(self):
        """会话 cwd -> 归并后的项目根目录。

        先剥掉 worktree 后缀，再把子目录里的会话归并到最外层的
        祖先项目（即本身也有会话的最短祖先路径），
        如 <proj>/web -> <proj>、<proj>/desktop_app -> <proj>。
        """
        cwds = {project_root(s.cwd) for s in self.sessions.values() if s.cwd}
        norm = {c: c.replace("\\", "/").rstrip("/").lower() for c in cwds}

        def root_of(cwd):
            n = norm[cwd]
            best = None   # 最短祖先 = 根目录
            for other, on in norm.items():
                if on != n and n.startswith(on + "/"):
                    if best is None or len(on) < len(norm[best]):
                        best = other
            return best or cwd

        return {c: root_of(c) for c in cwds}

    def project_of(self, sess, pmap):
        return pmap.get(project_root(sess.cwd))

    def known_project_cwd(self, cwd):
        if not isinstance(cwd, str) or not cwd:
            return False
        try:
            target = Path(cwd).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        with self.lock:
            pmap = self.project_map()
            known = {Path(s.cwd).expanduser().resolve() for s in self.sessions.values() if s.cwd}
            known.update(Path(project_root(s.cwd)).expanduser().resolve()
                         for s in self.sessions.values() if s.cwd)
            known.update(Path(root).expanduser().resolve() for root in pmap.values() if root)
            known.update(Path(h.cwd).expanduser().resolve() for h in self.hosted.values() if h.cwd)
        return target in known and target.is_dir()

    # ---------- 远程给会话发消息 ----------
    def _selected_model(self, sid, model=UNSET):
        if model is UNSET:
            return self.preferences.get(sid)
        model = validate_model(model)
        self.preferences.set(sid, model)
        return model

    def send_message(self, sid, text, force=False, model=UNSET):
        with self.lock:
            h = self.hosted_by_sid.get(sid)
            sess = self.by_sid(sid)
        cwd = h.cwd if h and h.status != "exited" else sess.cwd if sess else None
        if not sess and not h:
            return 404, {"error": "session not found"}
        if sess and sess.is_subagent:
            return 400, {"error": "子代理会话只读，不能发送消息"}
        if not cwd or not os.path.isdir(cwd):
            return 400, {"error": f"session cwd unavailable: {cwd}"}
        with self.cc_switch.operation(cwd):
            return self._send_message_locked(sid, text, force, model)

    def _send_message_locked(self, sid, text, force=False, model=UNSET):
        """给会话发消息；model 是该会话后续发送的模型覆盖。"""
        try:
            selected = validate_model(self.preferences.get(sid) if model is UNSET else model)
        except ValueError as e:
            return 400, {"error": str(e)}
        h = self.hosted_by_sid.get(sid)
        if h and h.needs_rehost and h.status != "exited":
            return 409, {"error": "项目 local 配置已更改；请退出托管后重新托管以加载新配置"}
        if h and h.status != "exited" and (h.alive() or h.control_failed or h.stopping):
            try:
                if model is not UNSET:
                    self.preferences.set(sid, selected)
                return h.send(text, selected)
            except ValueError as e:
                return 500, {"error": str(e)}
        with self.lock:
            sess = self.by_sid(sid)
            if not sess:
                return 404, {"error": "session not found"}
            if not sess.cwd or not os.path.isdir(sess.cwd):
                return 400, {"error": f"session cwd unavailable: {sess.cwd}"}
            if sid in self.busy:
                return 409, {"error": "a remote message is already running for this session"}
            if sess.summary(self.stale_after)["state"] == "working" and not force:
                return 409, {"error": "session is working (maybe in use in a terminal); "
                                      "retry when idle, or pass force=true"}
            self.busy.add(sid)
        try:
            if model is not UNSET:
                self.preferences.set(sid, selected)
        except ValueError as e:
            with self.lock:
                self.busy.discard(sid)
            return 500, {"error": str(e)}
        job = self._new_job(sid, text, selected)
        job["status"] = "running"
        threading.Thread(target=self._run_job, args=(sess, job, text, selected), daemon=True).start()
        return 202, {"job_id": job["id"], "status": "running"}

    def _run_job(self, sess, job, text, selected_model=None):
        self.emit(sess, "remote_message_sent", {"job": job["id"], "text": job["text"]})
        cmd = [self.claude_bin, "-p", "--resume", sess.id, "--output-format", "json"]
        cmd += model_args(self.claude_args, selected_model)
        print(f"[msg] job {job['id']} 会话 {sess.id} 开始: {job['text']}", flush=True)
        self._exec(job, cmd, sess.cwd, text)
        with self.lock:
            self.busy.discard(sess.id)
        print(f"[msg] job {job['id']} {job['status']}"
              + (f": {job['error']}" if job["error"] else f" {clip(job['result'] or '', 120)}"), flush=True)
        self.emit(sess, "remote_message_done", {"job": job["id"], "status": job["status"],
                                                "error": job["error"],
                                                "result": clip(job["result"] or "", 300)})

    def _exec(self, job, cmd, cwd, text):
        """同步跑一轮 `claude -p`，把结果写进 job（status/finished_at 在此收尾）。"""
        try:
            # 环境变量原样继承，所以 ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY 等自定义配置同样生效
            # 显式 utf-8：Windows 中文系统默认是 gbk，会把中文消息和回复弄乱
            p = subprocess.run(cmd, input=text, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=cwd, timeout=self.msg_timeout)
            out = p.stdout.strip()
            try:
                data = json.loads(out)
                job["result"] = data.get("result")
                job["result_session_id"] = data.get("session_id")
                if data.get("is_error") or p.returncode != 0:
                    job["error"] = clip(data.get("result") or p.stderr, 500)
            except json.JSONDecodeError:
                job["result"] = clip(out, 2000)
                if p.returncode != 0:
                    job["error"] = clip(p.stderr or out, 500)
        except FileNotFoundError:
            job["error"] = f"找不到命令 {self.claude_bin!r}，请用 --claude-bin 指定路径"
        except subprocess.TimeoutExpired:
            job["error"] = f"超时（{self.msg_timeout}s）"
        except Exception as e:
            job["error"] = str(e)
        finally:
            job["status"] = "failed" if job["error"] else "done"
            job["finished_at"] = now_ts()

    def new_session(self, cwd, text, model=UNSET):
        """在 cwd 启动一个全新会话；返回 job_id。"""
        if not isinstance(cwd, str) or not cwd or not os.path.isdir(cwd):
            return 400, {"error": f"cwd unavailable: {cwd}"}
        try:
            selected = validate_model(None if model is UNSET else model)
        except ValueError as e:
            return 400, {"error": str(e)}
        operation = self.cc_switch.operation(cwd)
        with operation:
            key = os.path.normcase(str(Path(cwd).resolve()))
            with self.lock:
                if key in self.new_cwd_busy:
                    return 409, {"error": "该目录的新会话仍在运行，请稍后再试"}
                self.new_cwd_busy.add(key)
            job = self._new_job(None, text, selected)
            job["status"] = "running"
            threading.Thread(target=self._run_new_job, args=(cwd, job, text, selected, key), daemon=True).start()
        return 202, {"job_id": job["id"], "status": "running"}

    def _run_new_job(self, cwd, job, text, selected_model=None, cwd_key=None):
        try:
            cmd = [self.claude_bin, "-p", "--output-format", "json"]
            cmd += model_args(self.claude_args, selected_model)
            print(f"[msg] job {job['id']} 新会话 (cwd={cwd}) 开始: {job['text']}", flush=True)
            self._exec(job, cmd, cwd, text)
            sid = job.get("result_session_id")
            if sid and selected_model is not None:
                try:
                    self.preferences.bind(sid, selected_model)
                except ValueError as e:
                    job["preference_error"] = str(e)
            print(f"[msg] job {job['id']} {job['status']} 新会话={sid}"
                  + (f": {job['error']}" if job["error"] else ""), flush=True)
        finally:
            if cwd_key:
                with self.lock:
                    self.new_cwd_busy.discard(cwd_key)

    def _new_job(self, sid, text, selected_model=None):
        job = {"id": secrets.token_hex(6), "session": sid, "status": "queued",
               "started_at": now_ts(), "finished_at": None, "text": clip(text, 200),
               "requested_model": selected_model,
               "result": None, "error": None, "result_session_id": None}
        with self.lock:
            self.jobs[job["id"]] = job
            for old in list(self.jobs)[:-100]:      # 只保留最近 100 个任务
                self.jobs.pop(old, None)
        return job

    def host_session(self, cwd, resume_id, text, force=False, model=UNSET, approval=None):
        with self.lock:
            sess = self.by_sid(resume_id) if resume_id else None
        target = cwd or (sess.cwd if sess else None)
        if not isinstance(target, str) or not target or not os.path.isdir(target):
            return 400, {"error": f"cwd unavailable: {target}"}
        with self.cc_switch.operation(target):
            return self._host_session_locked(cwd, resume_id, text, force, model, approval)

    def _host_session_locked(self, cwd, resume_id, text, force=False, model=UNSET, approval=None):
        """启动并托管常驻进程；resume_id 为空则新建会话。"""
        if approval is not None and approval not in APPROVAL_MODES:
            return 400, {"error": "approval 必须是 ask / auto / full"}
        try:
            selected = validate_model(self.preferences.get(resume_id) if model is UNSET and resume_id
                                     else None if model is UNSET else model)
        except ValueError as e:
            return 400, {"error": str(e)}
        with self.lock:
            sess = self.by_sid(resume_id) if resume_id else None
            if resume_id and not sess:
                return 404, {"error": "session not found"}
            if sess and sess.is_subagent:
                return 400, {"error": "子代理会话只读，不能托管"}
            old = self.hosted_by_sid.get(resume_id) if resume_id else None
            if old and old.proc and old.proc.poll() is None:
                return 409, {"error": "session is already hosted", "hosted_id": old.hid}
            if resume_id and resume_id in self.busy:
                return 409, {"error": "a remote message is already running for this session"}
            cwd = cwd or (sess.cwd if sess else None)
            if not cwd or not os.path.isdir(cwd):
                return 400, {"error": f"cwd unavailable: {cwd}"}
            if sess and sess.summary(self.stale_after)["state"] == "working" and not force:
                return 409, {"error": "session is working (maybe open in a terminal); "
                                      "close it first, or pass force=true"}
        if model is not UNSET and resume_id:
            try:
                self.preferences.set(resume_id, selected)
            except ValueError as e:
                return 500, {"error": str(e)}
        h = Hosted(self, cwd, resume_id, selected,
                   approval=approval or (self.approvals.get(resume_id) if resume_id else "ask"))
        try:
            code, resp = h.start(text)
        except FileNotFoundError:
            return 500, {"error": f"找不到命令 {self.claude_bin!r}，请用 --claude-bin 指定路径"}
        except Exception as e:
            return 500, {"error": str(e)}
        with self.lock:
            self.hosted[h.hid] = h
            if resume_id:
                self.hosted_by_sid[resume_id] = h
        resp["hosted_id"] = h.hid
        print(f"[hosted] {h.hid} 启动 (cwd={cwd}, resume={resume_id or '新会话'})", flush=True)
        return code, resp

    # ---------- 权限审批 ----------
    def pending_interactions(self, sid=None):
        with self.lock:
            items = [i.public() for i in self.interactions.values()
                     if i.state == "pending" and (sid is None or i.session_id == sid)]
        return sorted(items, key=lambda item: item["created_at"])

    def interrupt_session(self, sid):
        with self.lock:
            h = self.hosted_by_sid.get(sid)
        if not h or not h.alive():
            return 409, {"error": "session is not hosted"}
        return h.interrupt()

    def respond_interaction(self, iid, body):
        if not isinstance(body, dict):
            return 400, {"error": "invalid JSON body"}
        behavior = body.get("behavior")
        if behavior not in ("allow", "deny"):
            return 400, {"error": "behavior 必须是 allow 或 deny"}
        scope = body.get("scope", "once")
        if scope not in ("once", "always"):
            return 400, {"error": "scope 必须是 once 或 always"}
        if scope == "always" and behavior != "allow":
            return 400, {"error": "scope=always 只能用于 allow"}
        with self.lock:
            inter = self.interactions.get(iid)
            if inter is None:
                return 404, {"error": "interaction not found"}
            if inter.state != "pending":
                return 409, {"error": "interaction already resolved"}
            if scope == "always" and not inter.allow_always:
                return 400, {"error": "该请求不允许始终允许"}
        reason = body.get("reason")
        reason = clip(reason, 500) if isinstance(reason, str) else None
        inter.resolve(behavior, scope, reason)
        return 200, inter.public()

    # ---------- 事件分发 ----------
    def emit(self, sess, kind, data):
        if not self.first_scan_done:   # 首次扫描只回放历史状态，不推送
            return
        ev = {"ts": now_ts(), "session": sess.id, "type": kind, "data": data}
        sess.recent.append(ev)
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        if self.webhook:
            threading.Thread(target=self._post, args=(ev,), daemon=True).start()

    def _post(self, ev):
        try:
            req = urllib.request.Request(
                self.webhook,
                data=json.dumps(ev, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            print(f"[webhook] 失败: {e}")

    # ---------- 解析一行转录 ----------
    def _set_working(self, sess):
        sess.state = "working"

    def _set_idle(self, sess):
        if sess.state == "idle":
            return
        sess.state = "idle"
        self.emit(sess, "turn_finished", {"last": sess.last_assistant})

    def handle_line(self, sess, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        sess.cwd = obj.get("cwd") or sess.cwd
        if obj.get("gitBranch"):
            sess.git_branch = obj["gitBranch"]
        if obj.get("version"):
            sess.version = obj["version"]
        if obj.get("attributionAgent") and not sess.agent_name:
            sess.agent_name = obj["attributionAgent"]
        ts = obj.get("timestamp")
        ts_epoch = _parse_iso(ts)
        if ts_epoch:
            if sess.first_ts is None or ts_epoch < sess.first_ts:
                sess.first_ts = ts_epoch
            sess.last_event_ts = max(sess.last_event_ts, ts_epoch)
        elif self.first_scan_done:
            sess.last_event_ts = now_ts()
        typ = obj.get("type")

        if typ == "ai-title":
            sess.ai_title = obj.get("aiTitle") or sess.ai_title
            sess.title = sess.custom_title or sess.ai_title or sess.title
            return
        if typ == "custom-title":
            value = obj.get("customTitle")
            if value is not None:
                sess.custom_title = value or None
                sess.title = sess.custom_title or sess.ai_title or sess.title
            return
        if typ == "system":
            subtype = obj.get("subtype")
            if subtype == "turn_duration":
                sess.saw_turn_duration = True
                sess.last_turn_end_ts = ts_epoch or now_ts()
                if not sess.pending_tools and not sess.active_agents:
                    self._set_idle(sess)
            elif subtype == "compact_boundary":
                sess.compacted_at = ts_epoch or now_ts()
                self.emit(sess, "context_compacted", {"at": sess.compacted_at})
            return

        msg = obj.get("message") or {}
        content = msg.get("content")
        sidechain = bool(obj.get("isSidechain"))

        if typ == "user":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            kind = classify_user_line(obj, blocks)
            if kind == "meta":
                return
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    self._on_tool_result(sess, b)
            if kind == "user" and not sidechain:
                text = " ".join(b.get("text", "") for b in blocks
                                if isinstance(b, dict) and b.get("type") == "text")
                if text:
                    sess.user_msgs += 1
                    sess.last_input_ts = ts_epoch or now_ts()
                    if sess.first_user is None:
                        sess.first_user = clip(text, 80)
                    sess.last_user = clip(text)
                    self._set_working(sess)
                    self.emit(sess, "user_message", {"text": sess.last_user})
            elif sidechain or kind in ("tool", "injected", "system", "meta_tool"):
                # 子代理、工具回传和注入内容都说明会话仍在活动
                self._set_working(sess)

        elif typ == "assistant":
            if not sidechain:
                sess.assistant_msgs += 1
                if msg.get("model"):
                    sess.model = msg["model"]
            usage = msg.get("usage") or {}
            sess.tokens["input"] += usage.get("input_tokens", 0)
            sess.tokens["output"] += usage.get("output_tokens", 0)
            sess.tokens["cache_read"] += usage.get("cache_read_input_tokens", 0)
            sess.tokens["cache_creation"] += usage.get("cache_creation_input_tokens", 0)
            ctx = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                   + usage.get("cache_read_input_tokens", 0) + usage.get("output_tokens", 0))
            if ctx > 0:
                sess.context_tokens = ctx
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text") and not sidechain:
                    sess.last_assistant = clip(b["text"])
                    self.emit(sess, "assistant_message", {"text": sess.last_assistant})
                elif b.get("type") == "tool_use":
                    self._on_tool_use(sess, b, sidechain)
            if obj.get("isApiErrorMessage"):
                self.emit(sess, "api_error", {"text": clip(_result_text(msg), 300)})
                self._set_working(sess)
            elif sidechain:
                # 子代理（sidechain）在跑：即使它本回合 end_turn，主对话仍在等它的结果
                self._set_working(sess)
            elif not sess.saw_turn_duration and msg.get("stop_reason") == "end_turn" \
                    and not sess.pending_tools:
                self._set_idle(sess)

    def _on_tool_use(self, sess, b, sidechain=False):
        name, inp, tid = b.get("name"), b.get("input") or {}, b.get("id")
        sess.tool_uses[name] += 1
        if not sidechain and tid:
            sess.pending_tools.add(tid)
        self.emit(sess, "tool_use", {"tool": name, "input": clip(inp, 200)})
        if name == "Agent":
            if tid:
                sess.active_agents.add(tid)
            sub = {"type": inp.get("subagent_type") or "general-purpose",
                   "description": clip(inp.get("description", ""), 120),
                   "ts": now_ts()}
            sess.subagents.append(sub)
            self.emit(sess, "subagent_start", sub)
        elif name == "TaskCreate":
            sess.pending_create[tid] = inp.get("subject", "")
            sess.task_seq += 1
            task_id = str(sess.task_seq)  # Claude Code 任务 id 从 1 递增；若有 tasks 目录会被覆盖校正
            sess.tasks[task_id] = {"id": task_id, "subject": inp.get("subject", ""),
                                   "description": inp.get("description", ""), "status": "pending"}
            self.emit(sess, "task_created", sess.tasks[task_id])
        elif name == "TaskUpdate":
            task = sess.tasks.get(str(inp.get("taskId")))
            if task is not None:
                for k in ("status", "subject", "description"):
                    if k in inp:
                        task[k] = inp[k]
                if task["status"] == "deleted":
                    sess.tasks.pop(task["id"], None)
                self.emit(sess, "task_updated", {"id": task["id"], "status": inp.get("status"),
                                                 "subject": task["subject"]})

    def _on_tool_result(self, sess, b):
        sess.active_agents.discard(b.get("tool_use_id"))
        sess.pending_tools.discard(b.get("tool_use_id"))
        if b.get("is_error"):
            self.emit(sess, "tool_error", {"tool_use_id": b.get("tool_use_id"),
                                           "error": clip(b.get("content", ""), 200)})

    # ---------- 轮询文件 ----------
    def scan_once(self):
        root = CLAUDE_DIR / "projects"
        if not root.exists():
            return
        paths = list(root.glob("*/*.jsonl"))
        # 子代理转录：projects/<proj>/<parent-sid>/subagents/agent-*.jsonl
        paths.extend(root.glob("*/*/subagents/agent-*.jsonl"))
        for path in paths:
            is_subagent = path.parent.name == "subagents"
            if is_subagent:
                parent_id = path.parent.parent.name
                sid = f"{parent_id}::agent-{path.stem}"
            else:
                parent_id, sid = None, path.stem
            key = str(path)
            with self.lock:
                sess = self.sessions.get(key)
                if sess is None:
                    sess = self.sessions[key] = Session(sid, path)
                    sess.is_subagent = is_subagent
                    sess.parent_id = parent_id
                    self.emit(sess, "session_discovered", {"path": str(path)})
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size < sess.offset:      # 文件被截断/重写
                    sess.offset, sess.buf = 0, b""
                if size == sess.offset:
                    continue
                with open(path, "rb") as f:
                    f.seek(sess.offset)
                    chunk = f.read()
                    sess.offset += len(chunk)
                data = sess.buf + chunk
                *lines, sess.buf = data.split(b"\n")   # 最后一段可能是半行，留到下次
                for ln in lines:
                    if ln.strip():
                        self.handle_line(sess, ln.decode("utf-8", "replace"))
        self._merge_task_dirs()

    def _merge_task_dirs(self):
        """~/.claude/tasks/<sid>/*.json 若存在，以它为准校正任务列表。"""
        troot = CLAUDE_DIR / "tasks"
        if not troot.exists():
            return
        with self.lock:
            for sess in self.sessions.values():
                d = troot / sess.id
                if not d.is_dir():
                    continue
                tasks = {}
                for fp in sorted(d.glob("*.json")):
                    try:
                        t = json.loads(fp.read_text())
                    except Exception:
                        continue
                    if isinstance(t, dict) and "id" in t:
                        tasks[str(t["id"])] = {"id": str(t["id"]), "subject": t.get("subject", ""),
                                               "description": t.get("description", ""),
                                               "status": t.get("status", "pending")}
                if tasks and tasks != sess.tasks:
                    sess.tasks = tasks

    def run(self, interval):
        while True:
            try:
                self.scan_once()
            except Exception as e:
                print(f"[scan] 错误: {e}")
            if not self.first_scan_done:
                self.first_scan_done = True
                print(f"[monitor] 初次扫描完成，发现 {len(self.sessions)} 个会话")
            time.sleep(interval)


class Hosted:
    """服务自己启动并长期持有的 claude 进程（stream-json 双向通信）。

    消息写进同一个进程的 stdin；一轮结束（收到 result 事件）后再发下一条，
    模型忙的时候新消息先在这里排队。会话照常写 ~/.claude/projects 的转录文件，
    所以原有的监听逻辑无需改动。
    """

    def __init__(self, mon, cwd, resume_id=None, selected_model=None, approval=None):
        self.mon, self.cwd, self.resume_id = mon, cwd, resume_id
        self.hid = "h-" + secrets.token_hex(4)
        self.session_id = resume_id
        self.selected_model = selected_model
        self.approval = approval or (mon.approvals.get(resume_id) if resume_id else "ask")
        self.process_model = selected_model if selected_model is not None else default_model(mon.claude_args)
        self.proc = None
        self.stderr_thread = None
        self.lock = threading.RLock()
        self.ready = threading.Condition(self.lock)
        self.write_lock = threading.Lock()
        self.pending = deque()          # (text, job, model)
        self.current = None             # 正在执行的 job
        self.processing = False          # 正在控制握手/切换模型或等待本轮结果
        self.status = "starting"        # starting | running | exited
        self.returncode = None
        self.turns = 0
        self.started_at = now_ts()
        self.stderr_tail = deque(maxlen=30)
        self.control_timeout = 60
        self.control_seq = 0
        self.control_waiters = {}
        self.initialized = False
        self.stopping = False
        self.control_failed = False
        self.needs_rehost = False
        self.interrupting = False
        self.lifecycle_seen = False
        self.max_queued = 32

    def alive(self):
        return self.proc is not None and self.status == "running" and not self.stopping

    def _sess(self):
        with self.mon.lock:
            return self.mon.by_sid(self.session_id)

    def start(self, first_text):
        cmd = [self.mon.claude_bin, "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose",
               "--permission-prompt-tool", "stdio"]
        if self.resume_id:
            cmd += ["--resume", self.resume_id]
        else:
            # 预生成 session id，使首轮开始前就能关联审批和事件
            if not self.session_id:
                self.session_id = str(uuid.uuid4())
            cmd += ["--session-id", self.session_id]
        if self.approval == "auto" and not _arg_present(self.mon.claude_args, "--permission-mode"):
            cmd += ["--permission-mode", "auto"]
        cmd += model_args(self.mon.claude_args, self.selected_model)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=self.cwd)
        self.status = "running"
        threading.Thread(target=self._read_out, daemon=True).start()
        self.stderr_thread = threading.Thread(target=self._read_err, daemon=True)
        self.stderr_thread.start()
        threading.Thread(target=self._send_loop, daemon=True).start()
        return self.send(first_text, self.selected_model)

    def send(self, text, model=None):
        if self.needs_rehost:
            return 409, {"error": "项目 local 配置已更改；请退出托管后重新托管以加载新配置"}
        if self.status == "exited" or self.stopping or self.control_failed:
            return 409, {"error": "hosted process is stopping or cannot safely continue; rehost it"}
        try:
            model = validate_model(model)
        except ValueError as e:
            return 400, {"error": str(e)}
        job = self.mon._new_job(self.session_id or self.hid, text, model)
        job["command_uuid"] = str(uuid.uuid4())
        with self.ready:
            if self.status == "exited" or self.stopping or self.control_failed:
                job.update(status="failed", error="hosted process is stopping", finished_at=now_ts())
                return 409, {"error": job["error"]}
            if len(self.pending) >= self.max_queued:
                job.update(status="failed", error="hosted send queue is full", finished_at=now_ts())
                return 429, {"error": "hosted send queue is full"}
            self.pending.append((text, job, model))
            self.ready.notify_all()
        return 202, {"job_id": job["id"], "status": job["status"], "hosted": self.hid}

    def _write(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _control(self, subtype, **params):
        with self.lock:
            self.control_seq += 1
            request_id = f"monitor-{self.hid}-{self.control_seq}"
            event = threading.Event()
            self.control_waiters[request_id] = {"event": event}
        try:
            self._write({"type": "control_request", "request_id": request_id,
                         "request": {"subtype": subtype, **params}})
        except (BrokenPipeError, OSError) as e:
            with self.lock:
                self.control_waiters.pop(request_id, None)
            raise RuntimeError(f"控制请求写入失败: {e}") from e
        if not event.wait(self.control_timeout):
            with self.lock:
                self.control_waiters.pop(request_id, None)
            raise TimeoutError(f"控制请求超时: {subtype}")
        with self.lock:
            waiter = self.control_waiters.pop(request_id, {})
        if waiter.get("error"):
            raise RuntimeError(f"控制请求失败: {waiter['error']}")
        return waiter.get("response", {})

    def _send_loop(self):
        try:
            self._control("initialize", hooks=None)
            self.initialized = True
            while True:
                with self.ready:
                    while not self.pending and not self.stopping and self.status != "exited":
                        self.ready.wait()
                    if self.stopping or self.status == "exited":
                        break
                    text, job, model = self.pending.popleft()
                    self.processing = True
                try:
                    target = model if model is not None else default_model(self.mon.claude_args)
                    if target != self.process_model:
                        self._control("set_model", model=target)
                        self.process_model = target
                    with self.ready:
                        self.current = job
                        job["status"] = "running"
                    self._write({"type": "user", "uuid": job.get("command_uuid"),
                             "message": {"role": "user", "content": text}})
                except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as e:
                    job.update(status="failed", error=str(e), finished_at=now_ts())
                    with self.ready:
                        if self.current is job:
                            self.current = None
                        self.processing = False
                        self.control_failed = isinstance(e, (TimeoutError, BrokenPipeError, OSError)) or "控制" in str(e)
                        if self.control_failed:
                            self.stopping = True
                        self.ready.notify_all()
                    if self.control_failed:
                        break
                    continue
                with self.ready:
                    self.ready.wait_for(lambda: self.current is not job or self.stopping or self.status == "exited")
                    if self.current is job:
                        self.current = None
        except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as e:
            self.control_failed = True
            self.stopping = True
            print(f"[hosted] {self.hid} 控制失败: {e}", flush=True)
        finally:
            with self.ready:
                self.processing = False
                for _, job, _ in self.pending:
                    job.update(status="failed", error="托管进程无法安全发送后续消息", finished_at=now_ts())
                self.pending.clear()
                self.ready.notify_all()
            if self.control_failed and self.proc and self.proc.poll() is None:
                self.proc.terminate()

    def _read_out(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):        # 跳过 [claude-code:...] 之类的非 JSON 行
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = obj.get("type")
            if typ == "control_response":
                response = obj.get("response") or {}
                with self.ready:
                    waiter = self.control_waiters.get(response.get("request_id"))
                    if waiter:
                        if response.get("subtype") == "success":
                            waiter["response"] = response.get("response") or {}
                        else:
                            waiter["error"] = response.get("error") or "Claude 返回未知控制响应"
                        waiter["event"].set()
            elif typ == "control_request":
                if (obj.get("request") or {}).get("subtype") == "can_use_tool":
                    self._on_can_use_tool(obj)
            elif typ == "control_cancel_request":
                with self.mon.lock:
                    inter = self.mon.interactions.get(obj.get("request_id"))
                if inter:
                    inter.resolve("deny", reason="请求已被 Claude 取消", state="cancelled")
            elif typ == "system" and obj.get("subtype") == "init":
                self._on_init(obj.get("session_id"))
            elif typ == "command_lifecycle":
                self._on_lifecycle(obj)
            elif typ == "result":
                self._on_result(obj)
            # 其余事件（assistant / thinking 等）转录文件里已有，这里不处理
        rc = self.proc.wait()
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if self.stderr_thread and self.stderr_thread is not threading.current_thread():
            self.stderr_thread.join(timeout=1)
        try:
            if self.proc.stdout and not self.proc.stdout.closed:
                self.proc.stdout.close()
        except (OSError, ValueError):
            pass
        with self.ready:
            self.status, self.returncode = "exited", rc
            self.processing = False
            dead = ([self.current] if self.current else []) + [j for _, j, _ in self.pending]
            self.current = None
            self.pending.clear()
            tail = " | ".join(list(self.stderr_tail)[-3:])
            for waiter in self.control_waiters.values():
                waiter["error"] = "claude 进程已退出"
                waiter["event"].set()
            for job in dead:
                job.update(status="failed", finished_at=now_ts(),
                           error=job["error"] or f"claude 进程已退出 (code {rc}) {tail}")
            self.ready.notify_all()
        print(f"[hosted] {self.hid} 退出 code={rc} 会话={self.session_id}"
              + (f" stderr: {tail}" if tail else ""), flush=True)
        # 进程已退出：唤醒所有等待审批的线程，避免永久阻塞
        with self.mon.lock:
            waiting = [i for i in self.mon.interactions.values() if i.hosted_id == self.hid]
        for inter in waiting:
            inter.resolve("deny", reason="托管进程已退出", state="cancelled")
        sess = self._sess()
        if sess:
            self.mon.emit(sess, "hosted_exited", {"hosted": self.hid, "returncode": rc})

    def _read_err(self):
        try:
            for raw in self.proc.stderr:
                self.stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
        finally:
            try:
                self.proc.stderr.close()
            except (OSError, ValueError):
                pass

    def _on_init(self, sid):
        if not sid:
            return
        old = self.session_id
        self.session_id = sid
        with self.mon.lock:
            self.mon.hosted_by_sid[sid] = self
            if self.resume_id:
                self.mon.hosted_by_sid[self.resume_id] = self
            if old and old != sid:
                self.mon.hosted_by_sid.pop(old, None)
                for inter in self.mon.interactions.values():
                    if inter.hosted_id == self.hid:
                        inter.session_id = sid
        if self.selected_model is not None:
            try:
                self.mon.preferences.bind(sid, self.selected_model)
            except ValueError:
                pass

    def _emit_session_event(self, kind, data):
        sess = self._sess()
        if sess:
            self.mon.emit(sess, kind, data)

    def _on_lifecycle(self, obj):
        state = obj.get("state")
        cu = obj.get("command_uuid")
        self.lifecycle_seen = True
        if state == "started":
            with self.ready:
                job = self.current
                if job and job.get("command_uuid") == cu:
                    job["status"] = "running"
            return
        with self.ready:
            job = self.current if self.current and self.current.get("command_uuid") == cu else None
            if job:
                job["_terminal_state"] = state
        if job is None:
            self._emit_session_event("hosted_foreign_turn",
                                     {"hosted": self.hid, "state": state})
            return
        if state == "completed":
            if job.get("_result_seen"):
                self._finish_job(job, "completed")
            else:
                # result 可能稍后才到，留 3 秒收集回复内容
                threading.Timer(3.0, lambda: self._finish_job(job, "completed")).start()
        else:
            self._finish_job(job, state)

    def _finish_job(self, job, terminal_state):
        with self.ready:
            if job.get("finished_at"):
                return
            if terminal_state == "completed":
                status = "failed" if job.get("result_is_error") else "done"
                error = job.get("error")
            elif terminal_state == "cancelled":
                status, error = "cancelled", job.get("error") or "已中断"
            else:
                status, error = "failed", job.get("error") or f"command {terminal_state}"
            job.update(status=status, error=error, finished_at=now_ts())
            if self.current is job:
                self.current = None
            self.processing = False
            self.interrupting = False
            self.ready.notify_all()
            self.turns += 1
        print(f"[hosted] {self.hid} 第 {self.turns} 轮 {status} job={job.get('id')}"
              + (" 失败: " + error if error else ""), flush=True)
        self._emit_session_event("hosted_turn_done", {
            "hosted": self.hid, "job": job.get("id"), "status": status,
            "result": clip(job.get("result") or "", 300),
            "is_error": job.get("result_is_error", False)})

    def _on_result(self, obj):
        with self.ready:
            job = self.current
            if job is None:
                return
            job["result"] = obj.get("result")
            job["result_session_id"] = obj.get("session_id")
            job["cost_usd"] = obj.get("total_cost_usd")
            job["result_is_error"] = bool(obj.get("is_error"))
            job["_result_seen"] = True
            if obj.get("is_error"):
                job["error"] = clip(obj.get("result") or obj.get("subtype") or "error", 500)
            terminal = job.get("_terminal_state")
        if terminal:
            self._finish_job(job, terminal)
        else:
            # 旧版 CLI 或测试 fake 可能不发 command_lifecycle
            threading.Timer(3.0, lambda: self._finish_job(job, "completed")).start()

    # ---------- 权限审批 ----------
    def _on_can_use_tool(self, obj):
        request_id = obj.get("request_id")
        if not request_id:
            return
        inter = Interaction(request_id, self, obj.get("request") or {})
        with self.mon.lock:
            self.mon.interactions[request_id] = inter
        self._emit_session_event("interaction_pending", inter.public())
        if self.approval == "full":
            inter.resolve("allow", "once", state="allowed")
            self._write_interaction(inter)
        else:
            threading.Thread(target=self._await_interaction, args=(inter,), daemon=True).start()

    def _await_interaction(self, inter):
        timeout = self.mon.permission_timeout
        inter.event.wait(timeout if timeout and timeout > 0 else None)
        if inter.state == "pending":
            inter.resolve("deny", reason="审批超时，已自动拒绝", state="timed_out")
        self._write_interaction(inter)

    def _write_interaction(self, inter):
        if inter.behavior == "allow":
            response = {"behavior": "allow", "updatedInput": inter.updated_input}
            if inter.scope == "always":
                response["updatedPermissions"] = inter.suggestions
        else:
            response = {"behavior": "deny", "message": inter.message or "用户拒绝"}
        payload = {"type": "control_response", "response": {
            "subtype": "success", "request_id": inter.id, "response": response}}
        try:
            if self.status != "exited":
                self._write(payload)
        except (OSError, ValueError):
            if inter.state == "pending":
                inter.state = "cancelled"
        with self.mon.lock:
            self.mon.interactions.pop(inter.id, None)
        self._emit_session_event("interaction_resolved", inter.public())

    def set_approval(self, mode):
        if mode not in APPROVAL_MODES:
            raise ValueError("invalid approval mode")
        target = "auto" if mode == "auto" else "default"
        if self.approval == "auto" or mode == "auto":
            self._control("set_permission_mode", mode=target)
        self.approval = mode

    def interrupt(self):
        if not self.alive():
            return 409, {"error": "hosted process is not running"}
        with self.ready:
            self.interrupting = True
            for _, job, _ in self.pending:
                job.update(status="cancelled", error="已中断", finished_at=now_ts())
            self.pending.clear()
            job = self.current
            self.ready.notify_all()
        try:
            self._write({"type": "control_request",
                         "request_id": f"monitor-int-{self.hid}-{int(time.time() * 1000)}",
                         "request": {"subtype": "interrupt"}})
        except (OSError, ValueError) as exc:
            if job:
                self._finish_job(job, "failed")
            return 500, {"error": f"中断写入失败: {exc}"}
        if job:
            threading.Timer(30.0, lambda: self._finish_job(job, "cancelled")).start()
        else:
            with self.ready:
                self.interrupting = False
        return 200, {"interrupted": True, "queued_cancelled": True}

    def stop(self, kill=False):
        if self.proc is None or self.status == "exited":
            return
        with self.ready:
            self.stopping = True
            self.ready.notify_all()
            for _, job, _ in self.pending:
                job.update(status="failed", error="托管进程退出中，消息未发送", finished_at=now_ts())
            self.pending.clear()
        try:
            if kill:
                self.proc.terminate()
            else:
                self.proc.stdin.close()         # 关闭 stdin = 让它做完当前一轮后正常退出
                self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
        except Exception:
            pass

    def info(self):
        return {"hosted_id": self.hid, "session_id": self.session_id, "cwd": self.cwd,
                "status": self.status, "pid": self.proc.pid if self.proc else None,
                "busy": self.current is not None or self.processing, "queued": len(self.pending),
                "approval": self.approval, "interrupting": self.interrupting,
                "current_job": self.current.get("id") if self.current else None,
                "needs_rehost": self.needs_rehost,
                "turns": self.turns, "returncode": self.returncode,
                "started_at": self.started_at, "stderr_tail": list(self.stderr_tail)}


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


def permission_preview(tool_name, tool_input):
    """为审批卡片生成 diff / 命令 / JSON 预览。"""
    ti = tool_input or {}
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
    """一次等待用户回答的权限请求。"""

    def __init__(self, request_id, hosted, request):
        self.id = request_id
        self.hosted_id = hosted.hid
        self.session_id = hosted.session_id
        self.request = request
        self.tool_name = request.get("tool_name") or ""
        self.tool_use_id = request.get("tool_use_id")
        self.tool_input = request.get("input") or {}
        self.reason = strip_ansi(request.get("decision_reason") or "")
        self.suggestions = [s for s in (request.get("permission_suggestions") or [])
                            if isinstance(s, dict) and s.get("behavior") == "allow"]
        self.allow_always = bool(self.suggestions) and not request.get("suppress_always_allow_rule")
        self.preview = permission_preview(self.tool_name, self.tool_input)
        self.created_at = now_ts()
        self.event = threading.Event()
        self.behavior = None
        self.scope = "once"
        self.message = None
        self.updated_input = self.tool_input
        self.state = "pending"
        self.resolved_at = None

    def resolve(self, behavior, scope="once", reason=None, state=None):
        if self.state != "pending":
            return False
        self.behavior = behavior
        self.scope = scope
        self.message = reason
        self.state = state or ("allowed" if behavior == "allow" else "denied")
        self.resolved_at = now_ts()
        self.event.set()
        return True

    def public(self):
        return {"id": self.id, "hosted_id": self.hosted_id, "session_id": self.session_id,
                "tool_name": self.tool_name, "tool_use_id": self.tool_use_id,
                "tool_input": self.tool_input, "preview": self.preview,
                "reason": self.reason, "allow_always": self.allow_always,
                "created_at": self.created_at, "state": self.state,
                "behavior": self.behavior, "scope": self.scope,
                "resolved_at": self.resolved_at}


# 转录里 type=user 的行不一定是真人输入：工具结果回传、isMeta 注入、
# 命令/任务通知等系统文本也以 user 角色写入，展示时需区分开
SYSTEM_MARKERS = ("<task-notification>", "<command-name>", "<command-message>",
                  "<local-command-stdout>", "Caveat:", "[Request interrupted")


def classify_user_line(o, blocks):
    """把 type=user 的行分类。

    user      真人输入
    tool      工具结果回传
    meta      纯 boilerplate，展示和计数都应丢弃
    meta_tool isMeta + sourceToolUseID，内容属于对应工具卡片
    injected  origin/promptSource 注入的输入，不是真人输入
    system    命令输出、任务通知、中断标记等系统文本
    """
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return "tool"
    if o.get("isCompactSummary"):
        return "system"
    if o.get("isMeta"):
        return "meta_tool" if o.get("sourceToolUseID") else "meta"
    origin = o.get("origin")
    if (isinstance(origin, dict) and origin.get("kind")) or o.get("promptSource"):
        return "injected"
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            if (b.get("text") or "").lstrip().startswith(SYSTEM_MARKERS):
                return "system"
    return "user"


def _result_text(b):
    """提取 tool_result 的文本内容；content 可能是字符串或 text block 列表。"""
    c = b.get("content", "")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return c if isinstance(c, str) else str(c)


def _patch_text(patch):
    """把 structuredPatch 的 hunk 列表转成统一 diff 文本。"""
    lines = []
    for hunk in patch or []:
        if not isinstance(hunk, dict):
            continue
        lines.append(f"@@ -{hunk.get('oldStart', 0)},{hunk.get('oldLines', 0)} "
                     f"+{hunk.get('newStart', 0)},{hunk.get('newLines', 0)} @@")
        lines.extend(str(x) for x in (hunk.get("lines") or []))
    return "\n".join(lines)


def parse_tool_result(o, block):
    """把 tool_result 与行级 toolUseResult 合并成可展示结果。"""
    tur = o.get("toolUseResult")
    text = _result_text(block)
    kind = "error" if block.get("is_error") else "text"
    content = block.get("content")
    if isinstance(content, list) and any(
            isinstance(x, dict) and x.get("type") == "image" for x in content):
        kind, text = "image", "图片"
    if isinstance(tur, dict):
        patch = tur.get("structuredPatch")
        if isinstance(patch, list) and patch:
            kind, text = "diff", _patch_text(patch)
        elif "stdout" in tur or "stderr" in tur:
            chunks = []
            if tur.get("stdout"):
                chunks.append(str(tur["stdout"]))
            if tur.get("stderr"):
                chunks.append("[stderr]\n" + str(tur["stderr"]))
            if chunks:
                text = "\n".join(chunks)
        elif isinstance(tur.get("file"), dict):
            f = tur["file"]
            text = f"{f.get('filePath', '')} ({f.get('numLines', '?')} 行)"
        elif tur.get("type") == "image":
            kind, text = "image", str(tur.get("path") or tur.get("filePath") or "图片")
    return {"type": "tool_result", "tool_use_id": block.get("tool_use_id"),
            "is_error": bool(block.get("is_error")), "kind": kind,
            "text": clip(text, 8000)}


def parse_msg_line(ln, clip_len=8000):
    """把一行转录解析为一条消息；无内容返回 None。

    返回 {role: user/assistant/tool/system, ts, parts: [...]}。
    parts 类型：text / tool / tool_result / tool_note。
    """
    if isinstance(ln, bytes):
        ln = ln.decode("utf-8", "replace")
    if '"type"' not in ln:
        return None
    try:
        o = json.loads(ln)
    except json.JSONDecodeError:
        return None
    if o.get("isSidechain"):
        return None
    typ = o.get("type")
    if typ not in ("user", "assistant"):
        return None
    content = (o.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
    kind = classify_user_line(o, blocks) if typ == "user" else "assistant"
    if kind == "meta":
        return None
    role = {"user": "user", "injected": "system", "meta_tool": "system",
            "system": "system", "tool": "tool"}.get(kind, "assistant")
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and (b.get("text") or "").strip():
            if kind == "meta_tool":
                parts.append({"type": "tool_note",
                              "tool_use_id": o.get("sourceToolUseID"),
                              "text": clip(b["text"], 3000)})
            else:
                # 系统文本（任务通知/命令输出等）截短即可，不需要全文
                parts.append({"type": "text",
                              "text": clip(b["text"], 500 if role == "system" else clip_len)})
        elif bt == "tool_use":
            parts.append({"type": "tool", "tool_use_id": b.get("id"),
                          "name": b.get("name"), "input": clip(b.get("input") or {}, 500)})
        elif bt == "tool_result":
            parts.append(parse_tool_result(o, b))
    if parts:
        return {"role": role, "ts": _parse_iso(o.get("timestamp")), "parts": parts}
    return None


def make_handler(mon, refresh_interval):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def log_request(self, code="-", size="-"):
            # GET 轮询太频繁不打印；写操作和出错请求留日志便于排查
            if self.command != "GET" or (isinstance(code, int) and code >= 400):
                print(f"[http] {self.command} {self.path} -> {code}", flush=True)

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            self._raw(body, code)

        def _raw(self, body, code=200, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._json({"ok": True, "sessions": len(mon.sessions)})
            if u.path == "/interactions":
                sid = parse_qs(u.query).get("session", [None])[0]
                return self._json({"interactions": mon.pending_interactions(sid)})
            if u.path == "/":
                body = _jinja.get_template("index.html").render(
                    refresh_interval=refresh_interval,
                    stale_after=mon.stale_after,
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/sessions":
                # 默认返回概览（按项目分组所需字段齐全）；加 ?full=1 返回完整信息
                qs = parse_qs(u.query)
                full = qs.get("full", ["0"])[0] in ("1", "true")
                include_subagents = qs.get("include_subagents", ["0"])[0] in ("1", "true")
                with mon.lock:
                    pmap = mon.project_map()
                    items = sorted(((s.detail if full else s.summary)(mon.stale_after, mon.project_of(s, pmap))
                                    for s in mon.unique_sessions(include_subagents)),
                                   key=lambda x: x["last_input_at"] or x["last_event_at"],
                                   reverse=True)
                    counts = Counter(i.session_id for i in mon.interactions.values()
                                     if i.state == "pending")
                for it in items:
                    h = mon.hosted_by_sid.get(it["id"])
                    it["hosted"] = bool(h and h.alive())
                    it["hosted_id"] = h.hid if h else None
                    it["selected_model"] = mon.preferences.get(it["id"])
                    it["pending_interactions"] = counts.get(it["id"], 0)
                return self._json(items)
            if u.path.startswith("/sessions/") and u.path.endswith("/messages"):
                sid = u.path.split("/")[2]
                with mon.lock:
                    s = mon.by_sid(sid)
                if not s:
                    return self._json({"error": "not found"}, 404)
                try:
                    size = s.path.stat().st_size
                except OSError:
                    return self._json({"error": "file gone"}, 404)
                # 增量解析：只解析上次读过后新增的部分（自动刷新每 2-3 秒来一次，
                # 转录可达几十 MB，整文件重读既慢又占 CPU）
                st = mon.msg_cache.get(sid)
                if st is None or size < st["off"]:      # 首次或文件被截断
                    st = mon.msg_cache[sid] = {"off": 0, "buf": b"", "msgs": [],
                                               "body": None, "tools": {}}
                if size != st["off"]:
                    with open(s.path, "rb") as f:
                        f.seek(st["off"])
                        data = st["buf"] + f.read()
                    *lines, st["buf"] = data.split(b"\n")   # 半行留到下次拼接
                    for ln in lines:
                        m = parse_msg_line(ln)
                        if not m:
                            continue
                        keep = []
                        for part in m["parts"]:
                            if part.get("type") == "tool":
                                if part.get("tool_use_id"):
                                    st["tools"][part["tool_use_id"]] = part
                                keep.append(part)
                            elif part.get("type") == "tool_result":
                                target = st["tools"].get(part.get("tool_use_id"))
                                if target is not None:
                                    target["result"] = part
                                else:
                                    keep.append(part)
                            elif part.get("type") == "tool_note":
                                target = st["tools"].get(part.get("tool_use_id"))
                                if target is not None:
                                    target.setdefault("notes", []).append(part["text"])
                                else:
                                    keep.append({"type": "text", "text": part["text"]})
                            else:
                                keep.append(part)
                        if keep:
                            m["parts"] = keep
                            st["msgs"].append(m)
                    st["off"] = size
                    st["body"] = json.dumps({"id": sid, "size": size, "messages": st["msgs"]},
                                            ensure_ascii=False, separators=(",", ":")).encode()
                return self._raw(st["body"])
            if u.path.startswith("/sessions/"):
                sid = u.path.split("/", 2)[2]
                with mon.lock:
                    s = mon.by_sid(sid)
                    if not s:
                        return self._json({"error": "not found"}, 404)
                    d = s.detail(mon.stale_after, mon.project_of(s, mon.project_map()))
                d["selected_model"] = mon.preferences.get(sid)
                d["approval"] = mon.approvals.get(sid)
                d["interactions"] = mon.pending_interactions(sid)
                with mon.lock:
                    d["subagent_session_ids"] = sorted({s.id for s in mon.sessions.values()
                                                        if s.parent_id == sid})
                h = mon.hosted_by_sid.get(sid)
                d["hosted"] = bool(h and h.alive())
                d["hosted_id"] = h.hid if h else None
                d["hosted_busy"] = bool(h and h.alive()
                                        and (h.current or h.pending or h.processing))
                d["hosted_queued"] = len(h.pending) if h and h.alive() else 0
                return self._json(d)
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/cc-switch":
                cwd = parse_qs(u.query).get("cwd", [""])[0]
                if not mon.known_project_cwd(cwd):
                    return self._json({"error": "目标目录不属于已监控项目"}, 400)
                try:
                    return self._json(mon.cc_switch.status(cwd))
                except SwitchError as e:
                    return self._json({"error": e.message}, e.code)
            if u.path == "/hosted":
                return self._json([h.info() for h in mon.hosted.values()])
            if u.path.startswith("/jobs/"):
                job = mon.jobs.get(u.path.split("/", 2)[2])
                return self._json(job) if job else self._json({"error": "not found"}, 404)
            if u.path == "/events":
                return self._sse(parse_qs(u.query).get("session", [None])[0])
            self._json({"error": "not found"}, 404)

        def _read_body(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return None

        def _safe_json_write(self):
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._json({"error": "Content-Type 必须为 application/json"}, 415)
                return False
            site = self.headers.get("Sec-Fetch-Site", "").lower()
            if site == "cross-site":
                self._json({"error": "拒绝跨站写入请求"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                host = self.headers.get("Host", "")
                if parsed.netloc.lower() != host.lower():
                    self._json({"error": "Origin 与当前服务不匹配"}, 403)
                    return False
            return True

        def do_POST(self):
            u = urlparse(self.path)
            print(f"[http] {self.command} {self.path} 收到", flush=True)   # 请求到达即记录，响应见 log_request
            if u.path == "/cc-switch/local":
                if not self._safe_json_write():
                    return
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._json({"error": "invalid JSON body"}, 400)
                cwd, profile = body.get("cwd"), body.get("profile")
                if not mon.known_project_cwd(cwd):
                    return self._json({"error": "目标目录不属于已监控项目"}, 400)
                target = Path(cwd).expanduser().resolve()
                with mon.cc_switch.operation(cwd):
                    with mon.lock:
                        busy = (any(s.cwd and path_is_within(Path(s.cwd).expanduser().resolve(), target)
                                    and s.id in mon.busy for s in mon.sessions.values())
                                or any(path_is_within(p, target) for p in mon.new_cwd_busy))
                        hosts = list(mon.hosted.values())
                    busy = busy or any(h.cwd and path_is_within(Path(h.cwd).expanduser().resolve(), target)
                                       and (h.current or h.pending or h.processing) for h in hosts)
                    if busy:
                        return self._json({"error": "该目录有消息正在执行或排队，请完成后再切换配置"}, 409)
                    try:
                        result = mon.cc_switch.apply(cwd, profile, body.get("revision"),
                                                     confirm=body.get("confirm") is True)
                    except SwitchError as e:
                        return self._json({"error": e.message}, e.code)
                    for h in hosts:
                        try:
                            if path_is_within(Path(h.cwd).expanduser().resolve(), target) and h.status != "exited":
                                h.needs_rehost = True
                        except OSError:
                            continue
                    return self._json(result)
            if u.path == "/hosted":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._json({"error": "invalid JSON body"}, 400)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json({"error": "body must contain \"text\" (first message)"}, 400)
                code, resp = mon.host_session(body.get("cwd"), body.get("resume_id"),
                                              text, bool(body.get("force")),
                                              body.get("model", UNSET), body.get("approval"))
                return self._json(resp, code)
            if u.path.startswith("/interactions/") and u.path.endswith("/respond"):
                if not self._safe_json_write():
                    return
                iid = u.path.split("/")[2]
                code, resp = mon.respond_interaction(iid, self._read_body())
                return self._json(resp, code)
            if u.path == "/new":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._json({"error": "invalid JSON body"}, 400)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json({"error": "body must contain \"text\" (first message)"}, 400)
                code, resp = mon.new_session(body.get("cwd"), text, body.get("model", UNSET))
                return self._json(resp, code)
            parts = u.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "model":
                if not self._safe_json_write():
                    return
                body = self._read_body()
                if not isinstance(body, dict) or "model" not in body:
                    return self._json({"error": "body must contain model (string or null)"}, 400)
                with mon.lock:
                    sess = mon.by_sid(parts[1])
                if not sess:
                    return self._json({"error": "session not found"}, 404)
                try:
                    selected = mon.preferences.set(parts[1], body.get("model"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json({"selected_model": selected})
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "interrupt":
                if not self._safe_json_write():
                    return
                code, resp = mon.interrupt_session(parts[1])
                return self._json(resp, code)
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "approval":
                if not self._safe_json_write():
                    return
                body = self._read_body()
                mode = body.get("approval") if isinstance(body, dict) else None
                if mode not in APPROVAL_MODES:
                    return self._json({"error": "approval 必须是 ask / auto / full"}, 400)
                try:
                    mon.approvals.set(parts[1], mode)
                except ValueError as e:
                    return self._json({"error": str(e)}, 500)
                with mon.lock:
                    h = mon.hosted_by_sid.get(parts[1])
                if h and h.alive():
                    try:
                        h.set_approval(mode)
                    except (RuntimeError, TimeoutError) as e:
                        return self._json({"error": f"实时切换失败: {e}"}, 409)
                return self._json({"approval": mode})
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "message":
                body = self._read_body()
                if not isinstance(body, dict):
                    return self._json({"error": "invalid JSON body"}, 400)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json({"error": "body must be {\"text\": \"...\"}"}, 400)
                code, resp = mon.send_message(parts[1], text, bool(body.get("force")),
                                              body.get("model", UNSET))
                return self._json(resp, code)
            self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            u = urlparse(self.path)
            print(f"[http] {self.command} {self.path} 收到", flush=True)
            parts = u.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "hosted":
                h = mon.hosted.get(parts[1])
                if not h:
                    return self._json({"error": "not found"}, 404)
                h.stop(kill=parse_qs(u.query).get("kill", ["0"])[0] in ("1", "true"))
                return self._json(h.info())
            self._json({"error": "not found"}, 404)

        def _static(self, name):
            # 防目录穿越，只允许 static 目录下的普通文件
            fp = (STATIC_DIR / name).resolve()
            if not str(fp).startswith(str(STATIC_DIR.resolve())) or not fp.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = {".js": "application/javascript", ".css": "text/css",
                     ".html": "text/html", ".map": "application/json"}.get(fp.suffix, "application/octet-stream")
            body = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, only):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue(maxsize=1000)
            mon.subscribers.append(q)
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if only and ev["session"] != only:
                        continue
                    self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                mon.subscribers.remove(q)

    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔(秒)")
    ap.add_argument("--refresh", type=float, default=3.0, help="网页自动刷新间隔(秒)")
    ap.add_argument("--stale", type=int, default=300, help="多少秒无更新视为 stale")
    ap.add_argument("--webhook", help="把每个事件 POST 到该 URL")
    ap.add_argument("--claude-bin", default="claude", help="claude 命令路径")
    ap.add_argument("--claude-args", default="",
                    help='发消息时附加给 claude 的参数，例如 "--permission-mode acceptEdits --model sonnet"')
    ap.add_argument("--msg-timeout", type=int, default=600, help="单条远程消息最长执行秒数")
    ap.add_argument("--permission-timeout", type=int, default=300,
                    help="托管会话审批等待秒数，0 表示无限等待")
    args = ap.parse_args()

    mon = Monitor(args.stale, args.webhook, args.permission_timeout)
    # Windows 上 npm 安装的 claude 是 claude.cmd，这里先解析出完整路径
    mon.claude_bin = shutil.which(args.claude_bin) or args.claude_bin
    mon.claude_args = shlex.split(args.claude_args)
    mon.msg_timeout = args.msg_timeout
    threading.Thread(target=mon.run, args=(args.interval,), daemon=True).start()
    print(f"[monitor] 监听 {CLAUDE_DIR / 'projects'}，HTTP http://{args.host}:{args.port}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[monitor] 警告: 接口无鉴权且可远程执行 claude，请务必只暴露在可信网络/隧道之后")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(mon, args.refresh))
    try:
        server.serve_forever()
    finally:
        # 正常退出（含 Ctrl+C）时主动收尾托管进程：关 stdin 让它做完当前一轮再退，
        # 兜不住的强杀场景由管道 EOF 兜底——monitor 一死内核关闭管道，claude 读到
        # EOF 同样会自行退出，不会残留僵尸/孤儿进程
        server.server_close()
        for h in list(mon.hosted.values()):
            try:
                h.stop()
                print(f"[hosted] {h.hid} 已随服务退出 (code {h.returncode})", flush=True)
            except Exception as e:
                print(f"[hosted] {h.hid} 退出失败: {e}", flush=True)


if __name__ == "__main__":
    main()
