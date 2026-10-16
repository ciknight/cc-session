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
        self.msg_cache = {}        # sid -> (file_size, messages)

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

        if typ == "ai-title":
            sess.title = obj.get("aiTitle") or sess.title

        elif typ == "user":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for b in blocks:
                if b.get("type") == "tool_result":
                    self._on_tool_result(sess, b)
                elif b.get("type") == "text" and b.get("text"):
                    sess.user_msgs += 1
                    if sess.first_user is None:
                        sess.first_user = clip(b["text"], 80)
                    sess.last_user = clip(b["text"])
                    sess.state = "working"
                    self.emit(sess, "user_message", {"text": sess.last_user})
            if any(b.get("type") == "tool_result" for b in blocks):
                sess.state = "working"

        elif typ == "assistant":
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
                if b.get("type") == "text" and b.get("text"):
                    sess.last_assistant = clip(b["text"])
                    self.emit(sess, "assistant_message", {"text": sess.last_assistant})
                elif b.get("type") == "tool_use":
                    self._on_tool_use(sess, b)
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


def _parse_iso(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def extract_messages(path, clip_len=8000):
    """按需从转录文件提取完整对话（跳过子代理 sidechain 和工具结果）。

    返回 [{role: user/assistant, ts, parts: [{type: text|tool|error, ...}]}]
    """
    msgs = []
    with open(path, "rb") as f:
        for ln in f:
            if b'"type"' not in ln:
                continue
            try:
                o = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if o.get("isSidechain"):
                continue
            typ = o.get("type")
            if typ not in ("user", "assistant"):
                continue
            content = (o.get("message") or {}).get("content")
            parts = []
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    parts.append({"type": "text", "text": clip(b["text"], clip_len)})
                elif bt == "tool_use":
                    parts.append({"type": "tool", "name": b.get("name"),
                                  "input": clip(b.get("input") or {}, 200)})
                elif bt == "tool_result" and b.get("is_error"):
                    parts.append({"type": "error", "text": clip(b.get("content", ""), 300)})
            if parts:
                msgs.append({"role": typ, "ts": _parse_iso(o.get("timestamp")), "parts": parts})
    return msgs


def make_handler(mon, refresh_interval):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
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
                cached = mon.msg_cache.get(sid)
                if not cached or cached[0] != size:
                    mon.msg_cache[sid] = (size, extract_messages(s.path))
                return self._json({"id": sid, "size": size, "messages": mon.msg_cache[sid][1]})
            if u.path.startswith("/sessions/"):
                sid = u.path.split("/", 2)[2]
                with mon.lock:
                    s = mon.by_sid(sid)
                    if not s:
                        return self._json({"error": "not found"}, 404)
                    return self._json(s.detail(mon.stale_after, mon.project_of(s, mon.project_map())))
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/events":
                return self._sse(parse_qs(u.query).get("session", [None])[0])
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
    args = ap.parse_args()

    mon = Monitor(args.stale, args.webhook)
    threading.Thread(target=mon.run, args=(args.interval,), daemon=True).start()
    print(f"[monitor] 监听 {CLAUDE_DIR / 'projects'}，HTTP http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), make_handler(mon, args.refresh)).serve_forever()


if __name__ == "__main__":
    main()
