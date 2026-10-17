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
import json
import os
import queue
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

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


class Session:
    def __init__(self, sid, path):
        self.id = sid
        self.path = path
        self.cwd = None
        self.title = None            # ai-title
        self.model = None
        self.version = None          # Claude Code 版本
        self.git_branch = None
        self.offset = 0
        self.buf = b""
        self.tasks = {}              # taskId -> {id, subject, status}
        self.task_seq = 0
        self.pending_create = {}     # tool_use_id -> subject
        self.subagents = []          # Agent 工具调用: {type, description, ts}
        self.tool_uses = Counter()   # 工具名 -> 次数
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        self.user_msgs = 0
        self.assistant_msgs = 0
        self.first_ts = None
        self.first_user = None
        self.last_user = None
        self.last_assistant = None
        self.last_event_ts = 0
        self.state = "idle"
        self.recent = deque(maxlen=50)

    def summary(self, stale_after, project=None):
        state = self.state
        if self.last_event_ts and now_ts() - self.last_event_ts > stale_after:
            state = "stale"
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
        d["subagents"] = list(self.subagents)
        d["tool_uses"] = dict(self.tool_uses.most_common())
        d["recent_events"] = list(self.recent)
        return d


class Monitor:
    def __init__(self, stale_after, webhook=None):
        # 以文件路径为键：同一 session id 可能出现在多个项目目录（恢复/移动过），
        # 若按 id 键控，两个文件的读取 offset 会互相干扰导致重复计数
        self.sessions = {}           # str(path) -> Session
        self.stale_after = stale_after
        self.webhook = webhook
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
        self.hosted = {}           # hosted_id -> Hosted（服务托管的常驻进程）
        self.hosted_by_sid = {}    # session id -> Hosted

    def by_sid(self, sid):
        """按 session id 查找；同名取最近活跃的一个。"""
        best = None
        for s in self.sessions.values():
            if s.id == sid and (best is None or s.last_event_ts > best.last_event_ts):
                best = s
        return best

    def unique_sessions(self):
        """按 session id 去重后的会话列表（同名取最近活跃的）。"""
        best = {}
        for s in self.sessions.values():
            cur = best.get(s.id)
            if cur is None or s.last_event_ts > cur.last_event_ts:
                best[s.id] = s
        return list(best.values())

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

    # ---------- 远程给会话发消息 ----------
    def send_message(self, sid, text, force=False):
        """给会话发消息。若该会话已被托管，直接写入常驻进程；否则用
        `claude -p --resume <sid>` 在会话的工作目录里跑一轮。返回 (http_code, body)。"""
        h = self.hosted_by_sid.get(sid)
        if h and h.alive():
            return h.send(text)
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
        # 注意：_new_job 内部也要拿 self.lock，必须在锁外调用（Lock 不可重入，锁内调会死锁）
        job = self._new_job(sid, text)
        job["status"] = "running"
        threading.Thread(target=self._run_job, args=(sess, job, text), daemon=True).start()
        return 202, {"job_id": job["id"], "status": "running"}

    def _run_job(self, sess, job, text):
        self.emit(sess, "remote_message_sent", {"job": job["id"], "text": job["text"]})
        cmd = [self.claude_bin, "-p", "--resume", sess.id, "--output-format", "json"] + self.claude_args
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

    def new_session(self, cwd, text):
        """在 cwd 里用 `claude -p` 跑一轮全新会话（不托管，每条消息重启一次 claude）。
        新会话 id 在 job 完成后由 result_session_id 带回。返回 (http_code, body)。"""
        if not cwd or not os.path.isdir(cwd):
            return 400, {"error": f"cwd unavailable: {cwd}"}
        job = self._new_job(None, text)
        job["status"] = "running"
        threading.Thread(target=self._run_new_job, args=(cwd, job, text), daemon=True).start()
        return 202, {"job_id": job["id"], "status": "running"}

    def _run_new_job(self, cwd, job, text):
        cmd = [self.claude_bin, "-p", "--output-format", "json"] + self.claude_args
        print(f"[msg] job {job['id']} 新会话 (cwd={cwd}) 开始: {job['text']}", flush=True)
        self._exec(job, cmd, cwd, text)
        print(f"[msg] job {job['id']} {job['status']} 新会话={job['result_session_id']}"
              + (f": {job['error']}" if job["error"] else ""), flush=True)

    def _new_job(self, sid, text):
        job = {"id": secrets.token_hex(6), "session": sid, "status": "queued",
               "started_at": now_ts(), "finished_at": None, "text": clip(text, 200),
               "result": None, "error": None, "result_session_id": None}
        with self.lock:
            self.jobs[job["id"]] = job
            for old in list(self.jobs)[:-100]:      # 只保留最近 100 个任务
                self.jobs.pop(old, None)
        return job

    def host_session(self, cwd, resume_id, text, force=False):
        """启动并托管一个常驻 claude 进程；resume_id 为空则新建会话。返回 (http_code, body)。"""
        with self.lock:
            sess = self.by_sid(resume_id) if resume_id else None
            if resume_id and not sess:
                return 404, {"error": "session not found"}
            old = self.hosted_by_sid.get(resume_id) if resume_id else None
            if old and old.alive():
                return 409, {"error": "session is already hosted", "hosted_id": old.hid}
            if resume_id and resume_id in self.busy:
                return 409, {"error": "a remote message is already running for this session"}
            cwd = cwd or (sess.cwd if sess else None)
            if not cwd or not os.path.isdir(cwd):
                return 400, {"error": f"cwd unavailable: {cwd}"}
            if sess and sess.summary(self.stale_after)["state"] == "working" and not force:
                return 409, {"error": "session is working (maybe open in a terminal); "
                                      "close it first, or pass force=true"}
        h = Hosted(self, cwd, resume_id)
        try:
            code, resp = h.start(text)
        except FileNotFoundError:
            return 500, {"error": f"找不到命令 {self.claude_bin!r}，请用 --claude-bin 指定路径"}
        except Exception as e:
            return 500, {"error": str(e)}
        self.hosted[h.hid] = h
        resp["hosted_id"] = h.hid
        print(f"[hosted] {h.hid} 启动 (cwd={cwd}, resume={resume_id or '新会话'})", flush=True)
        return code, resp

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
        ts = obj.get("timestamp")
        ts_epoch = _parse_iso(ts)
        if ts_epoch and (sess.first_ts is None or ts_epoch < sess.first_ts):
            sess.first_ts = ts_epoch
        sess.last_event_ts = now_ts() if self.first_scan_done else ts_epoch or sess.last_event_ts
        typ = obj.get("type")
        msg = obj.get("message") or {}
        content = msg.get("content")
        sidechain = bool(obj.get("isSidechain"))

        if typ == "ai-title":
            sess.title = obj.get("aiTitle") or sess.title

        elif typ == "user":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            kind = classify_user_line(obj, blocks)
            for b in blocks:
                if b.get("type") == "tool_result":
                    self._on_tool_result(sess, b)
                elif b.get("type") == "text" and b.get("text") and kind == "user" and not sidechain:
                    # sidechain 里的 user 行是子代理的提示词，不是真人输入，不计入
                    sess.user_msgs += 1
                    if sess.first_user is None:
                        sess.first_user = clip(b["text"], 80)
                    sess.last_user = clip(b["text"])
                    sess.state = "working"
                    self.emit(sess, "user_message", {"text": sess.last_user})
            if any(b.get("type") == "tool_result" for b in blocks) or sidechain:
                sess.state = "working"

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
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if b.get("type") == "text" and b.get("text") and not sidechain:
                    sess.last_assistant = clip(b["text"])
                    self.emit(sess, "assistant_message", {"text": sess.last_assistant})
                elif b.get("type") == "tool_use":
                    self._on_tool_use(sess, b)
            if sidechain:
                # 子代理（sidechain）在跑：即使它本回合 end_turn，主对话仍在等它的结果，不算空闲
                sess.state = "working"
            else:
                sess.state = "idle" if msg.get("stop_reason") == "end_turn" else "working"
                if sess.state == "idle":
                    self.emit(sess, "turn_finished", {"last": sess.last_assistant})

    def _on_tool_use(self, sess, b):
        name, inp, tid = b.get("name"), b.get("input") or {}, b.get("id")
        sess.tool_uses[name] += 1
        self.emit(sess, "tool_use", {"tool": name, "input": clip(inp, 200)})
        if name == "Agent":
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
        if b.get("is_error"):
            self.emit(sess, "tool_error", {"tool_use_id": b.get("tool_use_id"),
                                           "error": clip(b.get("content", ""), 200)})

    # ---------- 轮询文件 ----------
    def scan_once(self):
        root = CLAUDE_DIR / "projects"
        if not root.exists():
            return
        for path in root.glob("*/*.jsonl"):
            sid = path.stem
            key = str(path)
            with self.lock:
                sess = self.sessions.get(key)
                if sess is None:
                    sess = self.sessions[key] = Session(sid, path)
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

    def __init__(self, mon, cwd, resume_id=None):
        self.mon, self.cwd, self.resume_id = mon, cwd, resume_id
        self.hid = "h-" + secrets.token_hex(4)
        self.session_id = resume_id
        self.proc = None
        self.lock = threading.Lock()
        self.pending = deque()          # (text, job)
        self.current = None             # 正在执行的 job
        self.status = "starting"        # starting | running | exited
        self.returncode = None
        self.turns = 0
        self.started_at = now_ts()
        self.stderr_tail = deque(maxlen=30)

    def alive(self):
        return self.proc is not None and self.status == "running"

    def _sess(self):
        with self.mon.lock:
            return self.mon.by_sid(self.session_id)

    def start(self, first_text):
        cmd = [self.mon.claude_bin, "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose"]
        if self.resume_id:
            cmd += ["--resume", self.resume_id]
        cmd += self.mon.claude_args
        # 用二进制管道自己处理编码和换行，避免 Windows 把 \n 改成 \r\n、或用 gbk 编码
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=self.cwd)
        self.status = "running"
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        return self.send(first_text)

    def send(self, text):
        if self.status == "exited":
            return 409, {"error": "hosted process has exited"}
        job = self.mon._new_job(self.session_id or self.hid, text)
        with self.lock:
            self.pending.append((text, job))
            self._pump()
        return 202, {"job_id": job["id"], "status": job["status"], "hosted": self.hid}

    def _pump(self):
        """调用时已持有 self.lock。上一轮没结束就不发，等 result 再发。"""
        if self.current is not None or not self.pending:
            return
        text, job = self.pending.popleft()
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            job.update(status="failed", error=f"写入失败: {e}", finished_at=now_ts())
            return
        job["status"] = "running"
        self.current = job

    def _read_out(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):        # 跳过 [claude-code:...] 之类的非 JSON 行
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                self._on_init(obj.get("session_id"))
            elif obj.get("type") == "result":
                self._on_result(obj)
            # 其余事件（assistant / thinking 等）转录文件里已有，这里不处理
        rc = self.proc.wait()
        with self.lock:
            self.status, self.returncode = "exited", rc
            dead = ([self.current] if self.current else []) + [j for _, j in self.pending]
            self.current = None
            self.pending.clear()
            tail = " | ".join(list(self.stderr_tail)[-3:])
            for job in dead:
                job.update(status="failed", finished_at=now_ts(),
                           error=job["error"] or f"claude 进程已退出 (code {rc}) {tail}")
        print(f"[hosted] {self.hid} 退出 code={rc} 会话={self.session_id}"
              + (f" stderr: {tail}" if tail else ""), flush=True)
        sess = self._sess()
        if sess:
            self.mon.emit(sess, "hosted_exited", {"hosted": self.hid, "returncode": rc})

    def _read_err(self):
        for raw in self.proc.stderr:
            self.stderr_tail.append(raw.decode("utf-8", "replace").rstrip())

    def _on_init(self, sid):
        if not sid:
            return
        self.session_id = sid
        self.mon.hosted_by_sid[sid] = self
        if self.resume_id:
            self.mon.hosted_by_sid[self.resume_id] = self

    def _on_result(self, obj):
        with self.lock:
            job, self.current = self.current, None
            self.turns += 1
            if job:
                job["result"] = obj.get("result")
                job["result_session_id"] = obj.get("session_id")
                if obj.get("is_error"):
                    job["error"] = clip(obj.get("result") or obj.get("subtype") or "error", 500)
                job["status"] = "failed" if job["error"] else "done"
                job["finished_at"] = now_ts()
            self._pump()
        print(f"[hosted] {self.hid} 第 {self.turns} 轮完成 job={job['id'] if job else '-'}"
              + (" 失败: " + job["error"] if job and job["error"] else ""), flush=True)
        sess = self._sess()
        if sess:
            self.mon.emit(sess, "hosted_turn_done", {
                "hosted": self.hid, "job": job["id"] if job else None,
                "result": clip(obj.get("result") or "", 300),
                "cost_usd": obj.get("total_cost_usd"), "is_error": obj.get("is_error")})

    def stop(self, kill=False):
        if self.proc is None or self.status == "exited":
            return
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
                "busy": self.current is not None, "queued": len(self.pending),
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


# 转录里 type=user 的行不一定是真人输入：工具结果回传、isMeta 注入、
# 命令/任务通知等系统文本也以 user 角色写入，展示时需区分开
SYSTEM_MARKERS = ("<task-notification>", "<command-name>", "<command-message>",
                  "<local-command-stdout>", "Caveat:", "[Request interrupted")


def classify_user_line(o, blocks):
    """把 type=user 的行分类为 user（真人）/ system（系统文本）/ tool（工具结果回传）。"""
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return "tool"
    if o.get("isMeta"):
        return "system"
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


def parse_msg_line(ln, clip_len=8000):
    """把一行转录解析为一条消息（跳过子代理 sidechain 和工具结果）；无内容返回 None。

    返回 {role: user/assistant/tool/system, ts, parts: [{type: text|tool|error, ...}]}
    """
    if b'"type"' not in ln:
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
    parts = []
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
    role = classify_user_line(o, blocks) if typ == "user" else "assistant"
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and b.get("text", "").strip():
            # 系统文本（任务通知/命令输出等）截短即可，不需要全文
            parts.append({"type": "text", "text": clip(b["text"], 500 if role == "system" else clip_len)})
        elif bt == "tool_use":
            parts.append({"type": "tool", "name": b.get("name"),
                          "input": clip(b.get("input") or {}, 200)})
        elif bt == "tool_result" and b.get("is_error"):
            parts.append({"type": "error", "text": clip(_result_text(b), 300)})
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
                full = parse_qs(u.query).get("full", ["0"])[0] in ("1", "true")
                with mon.lock:
                    pmap = mon.project_map()
                    items = sorted(((s.detail if full else s.summary)(mon.stale_after, mon.project_of(s, pmap))
                                    for s in mon.unique_sessions()),
                                   key=lambda x: x["last_event_at"], reverse=True)
                for it in items:
                    h = mon.hosted_by_sid.get(it["id"])
                    it["hosted"] = bool(h and h.alive())
                    it["hosted_id"] = h.hid if h else None
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
                    st = mon.msg_cache[sid] = {"off": 0, "buf": b"", "msgs": [], "body": None}
                if size != st["off"]:
                    with open(s.path, "rb") as f:
                        f.seek(st["off"])
                        data = st["buf"] + f.read()
                    *lines, st["buf"] = data.split(b"\n")   # 半行留到下次拼接
                    for ln in lines:
                        m = parse_msg_line(ln)
                        if m:
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
                h = mon.hosted_by_sid.get(sid)
                d["hosted"] = bool(h and h.alive())
                d["hosted_id"] = h.hid if h else None
                return self._json(d)
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
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

        def do_POST(self):
            u = urlparse(self.path)
            print(f"[http] {self.command} {self.path} 收到", flush=True)   # 请求到达即记录，响应见 log_request
            if u.path == "/hosted":
                body = self._read_body()
                if body is None:
                    return self._json({"error": "invalid JSON body"}, 400)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json({"error": "body must contain \"text\" (first message)"}, 400)
                code, resp = mon.host_session(body.get("cwd"), body.get("resume_id"),
                                              text, bool(body.get("force")))
                return self._json(resp, code)
            if u.path == "/new":
                body = self._read_body()
                if body is None:
                    return self._json({"error": "invalid JSON body"}, 400)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json({"error": "body must contain \"text\" (first message)"}, 400)
                code, resp = mon.new_session(body.get("cwd"), text)
                return self._json(resp, code)
            parts = u.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "message":
                body = self._read_body()
                if body is None:
                    return self._json({"error": "invalid JSON body"}, 400)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._json({"error": "body must be {\"text\": \"...\"}"}, 400)
                code, resp = mon.send_message(parts[1], text, bool(body.get("force")))
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
    args = ap.parse_args()

    mon = Monitor(args.stale, args.webhook)
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
