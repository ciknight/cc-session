#!/usr/bin/env python3
"""
claude_session_monitor.py — 监听 Claude Code 会话的任务内容和状态（纯标准库，无需安装依赖）

数据来源:
  ~/.claude/projects/<项目>/<sessionId>.jsonl   会话转录，逐行追加
  ~/.claude/tasks/<sessionId>/*.json            任务清单（如果存在，作为补充）

接口 (默认 http://127.0.0.1:8765):
  GET /                    网页看板：列出所有会话及任务，每 2 秒自动刷新
  GET /sessions            所有会话的完整信息（状态、消息、任务列表、最近事件），无需 id
                           加 ?brief=1 只返回概览
  GET /sessions/<id>       按 id 取单个会话（可选）
  GET /events              SSE 实时事件流（?session=<id> 可过滤）
  GET /healthz             健康检查

会话状态:
  working  最近有活动，且最后一个事件不是"助手完成回复"
  idle     助手已完成回复（stop_reason=end_turn），等待用户输入
  stale    超过 --stale 秒没有任何更新

用法:
  python3 claude_session_monitor.py                      # 监听默认目录
  python3 claude_session_monitor.py --port 9000 --stale 600
  python3 claude_session_monitor.py --webhook http://localhost:3000/hook   # 事件同时 POST 出去
"""
import argparse
import json
import os
import queue
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def now_ts():
    return time.time()


def clip(s, n=300):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


class Session:
    def __init__(self, sid, path):
        self.id = sid
        self.path = path
        self.cwd = None
        self.offset = 0
        self.buf = b""
        self.tasks = {}            # taskId -> {id, subject, status}
        self.task_seq = 0
        self.pending_create = {}   # tool_use_id -> subject
        self.last_user = None
        self.last_assistant = None
        self.last_event_ts = 0
        self.state = "idle"
        self.recent = deque(maxlen=50)

    def summary(self, stale_after):
        state = self.state
        if self.last_event_ts and now_ts() - self.last_event_ts > stale_after:
            state = "stale"
        done = sum(1 for t in self.tasks.values() if t["status"] == "completed")
        return {
            "id": self.id,
            "cwd": self.cwd,
            "state": state,
            "last_event_at": self.last_event_ts,
            "last_user_message": self.last_user,
            "last_assistant_message": self.last_assistant,
            "tasks_total": len(self.tasks),
            "tasks_completed": done,
        }

    def detail(self, stale_after):
        d = self.summary(stale_after)
        d["tasks"] = list(self.tasks.values())
        d["recent_events"] = list(self.recent)
        return d


class Monitor:
    def __init__(self, stale_after, webhook=None):
        self.sessions = {}
        self.stale_after = stale_after
        self.webhook = webhook
        self.lock = threading.Lock()
        self.subscribers = []      # list of queue.Queue
        self.first_scan_done = False

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
        ts = obj.get("timestamp")
        sess.last_event_ts = now_ts() if self.first_scan_done else _parse_iso(ts) or sess.last_event_ts
        typ = obj.get("type")
        msg = obj.get("message") or {}
        content = msg.get("content")

        if typ == "user":
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            for b in blocks:
                if b.get("type") == "tool_result":
                    self._on_tool_result(sess, b)
                elif b.get("type") == "text" and b.get("text"):
                    sess.last_user = clip(b["text"])
                    sess.state = "working"
                    self.emit(sess, "user_message", {"text": sess.last_user})
            if any(b.get("type") == "tool_result" for b in blocks):
                sess.state = "working"

        elif typ == "assistant":
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
        self.emit(sess, "tool_use", {"tool": name, "input": clip(inp, 200)})
        if name == "TaskCreate":
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
            with self.lock:
                sess = self.sessions.get(sid)
                if sess is None:
                    sess = self.sessions[sid] = Session(sid, path)
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
            for sid, sess in self.sessions.items():
                d = troot / sid
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


DASHBOARD_HTML = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Sessions</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--card:#f6f6f4;--mut:#777;--bd:#e2e2de}
@media(prefers-color-scheme:dark){:root{--bg:#1b1b1a;--fg:#eee;--card:#262625;--mut:#999;--bd:#3a3a38}}
body{font:14px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px;max-width:900px;margin:auto}
h1{font-size:18px}.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:12px;margin:10px 0}
.mut{color:var(--mut);font-size:12px}.b{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#fff}
.working{background:#d97706}.idle{background:#16a34a}.stale{background:#888}
ul{margin:6px 0;padding-left:18px}.completed{text-decoration:line-through;color:var(--mut)}.in_progress{font-weight:600}
</style></head><body>
<h1>Claude Sessions <span class="mut" id="n"></span></h1><div id="list"></div>
<script>
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
async function load(){
  try{
    const r=await fetch("/sessions");const d=await r.json();
    document.getElementById("n").textContent="("+d.length+")";
    document.getElementById("list").innerHTML=d.map(s=>`
      <div class="card">
        <span class="b ${s.state}">${s.state}</span> <b>${esc(s.cwd||"?")}</b>
        <span class="mut"> ${esc(s.id)} · ${s.last_event_at?new Date(s.last_event_at*1000).toLocaleString():""}</span>
        <div class="mut">用户: ${esc(s.last_user_message)}</div>
        <div class="mut">助手: ${esc(s.last_assistant_message)}</div>
        ${s.tasks.length?`<div>任务 ${s.tasks_completed}/${s.tasks_total}</div><ul>`+
          s.tasks.map(t=>`<li class="${t.status}">[${t.status}] ${esc(t.subject)}</li>`).join("")+"</ul>":""}
      </div>`).join("")||'<div class="mut">暂无会话</div>';
  }catch(e){}
}
load();setInterval(load,2000);
</script></body></html>"""


def make_handler(mon):
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
                body = DASHBOARD_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/sessions":
                # 默认返回所有会话的完整信息（含任务列表、最近事件），无需 session id
                # 加 ?brief=1 只返回概览
                brief = parse_qs(u.query).get("brief", ["0"])[0] in ("1", "true")
                with mon.lock:
                    items = sorted(((s.summary if brief else s.detail)(mon.stale_after)
                                    for s in mon.sessions.values()),
                                   key=lambda x: x["last_event_at"], reverse=True)
                return self._json(items)
            if u.path.startswith("/sessions/"):
                sid = u.path.split("/", 2)[2]
                with mon.lock:
                    s = mon.sessions.get(sid)
                    return self._json(s.detail(mon.stale_after)) if s else self._json({"error": "not found"}, 404)
            if u.path == "/events":
                return self._sse(parse_qs(u.query).get("session", [None])[0])
            self._json({"error": "not found"}, 404)

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
    ap.add_argument("--stale", type=int, default=300, help="多少秒无更新视为 stale")
    ap.add_argument("--webhook", help="把每个事件 POST 到该 URL")
    args = ap.parse_args()

    mon = Monitor(args.stale, args.webhook)
    threading.Thread(target=mon.run, args=(args.interval,), daemon=True).start()
    print(f"[monitor] 监听 {CLAUDE_DIR / 'projects'}，HTTP http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), make_handler(mon)).serve_forever()


if __name__ == "__main__":
    main()
