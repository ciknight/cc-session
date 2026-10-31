"""HTTP/SSE 层：REST 路由、AskUserQuestion hook 回调、SSE 事件流与静态资源。"""
import hmac
import json
import math
import queue
import secrets
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

from session_preferences import UNSET, validate_metadata_patch
from cc_switch import SwitchError

from . import config, system, util
from .commands import scan_slash_commands
from .interactions import Interaction
from .transcript import _align_line_start, _parse_message_chunk, _absorb_orphan_tool_parts
from .util import path_is_within

_jinja = Environment(
    loader=FileSystemLoader(str(config.TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def make_handler(mon, refresh_interval):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def log_request(self, code="-", size="-"):
            # GET 轮询太频繁不打印；写操作和出错请求留日志便于排查
            if self.command != "GET" or (isinstance(code, int) and code >= 400):
                # 请求行解析失败时 stdlib 只设置 command（None），没有 path，
                # 回退到 requestline（它在版本校验前就已赋值）
                path = getattr(self, "path", None) or getattr(self, "requestline", "?")
                print(f"[http] {self.command} {path} -> {code}", flush=True)

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
            self._raw(body, code)

        def _raw(self, body, code=200, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _index(self):
            body = _jinja.get_template("index.html").render(
                refresh_interval=refresh_interval,
                stale_after=mon.stale_after,
            ).encode()
            self._raw(body, ctype="text/html; charset=utf-8")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._json({"ok": True, "sessions": len(mon.sessions)})
            if u.path == "/system":
                try:
                    data = system.system_snapshot()
                except system.SystemInfoUnavailable as e:
                    # 采集失败也带剩余秒数：已设定的定时仍在跑，前端错误态要能显示/取消
                    return self._json({"error": f"系统信息不可用：{e}",
                                       "sleep_timer_remaining": mon.sleep_timer_remaining()}, 503)
                except Exception as e:
                    return self._json({"error": f"系统状态采集失败：{e}",
                                       "sleep_timer_remaining": mon.sleep_timer_remaining()}, 500)
                data["sleep_timer_remaining"] = mon.sleep_timer_remaining()
                return self._json(data)
            if u.path == "/interactions":
                sid = parse_qs(u.query).get("session", [None])[0]
                return self._json({"interactions": mon.pending_interactions(sid)})
            if u.path in ("/", "/pending", "/hosted") or u.path.startswith("/p/"):
                # /p/<项目>[/s/<会话>|/new] 都由前端路由裁决，服务端统一回模板
                return self._index()
            if u.path == "/sessions":
                # 默认返回概览（按项目分组所需字段齐全）；加 ?full=1 返回完整信息
                qs = parse_qs(u.query)
                full = qs.get("full", ["0"])[0] in ("1", "true")
                include_subagents = qs.get("include_subagents", ["0"])[0] in ("1", "true")
                if include_subagents:
                    # 子代理默认懒加载；显式请求时再扫描，首次可能较慢
                    mon.scan_subagents_once()
                with mon.lock:
                    pmap = mon.project_map()
                    items = sorted(((s.detail if full else s.summary)(mon.stale_after, mon.project_of(s, pmap))
                                    for s in mon.unique_sessions(include_subagents)),
                                   key=lambda x: x["last_input_at"] or x["last_event_at"],
                                   reverse=True)
                    counts = Counter(i.session_id for i in mon.interactions.values()
                                     if i.state == "pending")
                for it in items:
                    mon.with_metadata(it)
                    h = mon.hosted_by_sid.get(it["id"])
                    it["hosted"] = bool(h and h.alive())
                    it["hosted_id"] = h.hid if h else None
                    it["selected_model"] = mon.preferences.get(it["id"])
                    it["pending_interactions"] = counts.get(it["id"], 0)
                return self._json(items)
            parts = u.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "jobs":
                code, payload = mon.session_jobs(parts[1])
                return self._json(payload, code)
            if u.path.startswith("/sessions/") and u.path.endswith("/messages"):
                sid = u.path.split("/")[2]
                qs = parse_qs(u.query)
                try:
                    limit = int(qs.get("limit", ["100"])[0])
                    before = int(qs["before"][0]) if "before" in qs else None
                    frm = int(qs["from"][0]) if "from" in qs else None
                except ValueError:
                    return self._json({"error": "limit/before/from 必须是整数"}, 400)
                if limit < 1 or limit > 500 or (before is not None and before < 0) \
                        or (frm is not None and frm < 0):
                    return self._json({"error": "分页参数超出范围"}, 400)
                if before is not None and frm is not None:
                    return self._json({"error": "before 与 from 不能同时使用"}, 400)
                mon.ensure_subagent(sid)
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
                cache_key = str(s.path)
                with mon.msg_locks_guard:
                    msg_lock = mon.msg_locks.setdefault(cache_key, threading.Lock())
                    mon.msg_active[cache_key] += 1
                try:
                    with msg_lock:
                        st = mon.msg_cache.get(cache_key)
                        if st is None or size < st.get("off", 0) or size < st.get("base", 0):
                            # 首次（或文件被截断/替换）：大文件只解析尾部窗口
                            base = 0
                            if size > config.MSG_TAIL_BYTES:
                                with open(s.path, "rb") as f:
                                    base = _align_line_start(f, size - config.MSG_TAIL_BYTES, size)
                            st = mon.msg_cache[cache_key] = {"base": base, "off": base, "buf": b"",
                                                             "msgs": [], "tools": {}}
                        if size != st["off"]:
                            with open(s.path, "rb") as f:
                                f.seek(st["off"])
                                raw = f.read()
                            data = st["buf"] + raw
                            msgs, st["buf"] = _parse_message_chunk(data, st["tools"])
                            st["msgs"].extend(msgs)
                            st["off"] += len(raw)
                            size = st["off"]
                        shift = 0
                        if before is not None and before <= 0 and st["base"] > 0:
                            # 向前翻页：加载上一块并前插；旧窗口里找不到 tool_use 的
                            # 独立结果/备注并入新加载的工具条目，避免重复展示
                            total_before = len(st["msgs"])
                            prepended = 0
                            with open(s.path, "rb") as f:
                                for _ in range(32):
                                    new_base = _align_line_start(
                                        f, max(0, st["base"] - config.MSG_CHUNK_BYTES), st["base"])
                                    if new_base >= st["base"]:
                                        break
                                    f.seek(new_base)
                                    data = f.read(st["base"] - new_base)
                                    chunk_tools = {}
                                    chunk_msgs, _ = _parse_message_chunk(data, chunk_tools)
                                    if chunk_tools:
                                        st["msgs"], _ = _absorb_orphan_tool_parts(
                                            st["msgs"], chunk_tools)
                                    st["msgs"] = chunk_msgs + st["msgs"]
                                    st["tools"].update(chunk_tools)
                                    prepended += len(chunk_msgs)
                                    st["base"] = new_base
                                    if chunk_msgs or new_base == 0:
                                        break
                            before = prepended
                            # 吸收会删掉旧窗口的消息：净位移才是调用方游标的移动量
                            shift = len(st["msgs"]) - total_before
                        total = len(st["msgs"])
                        if before is not None:
                            start, end = max(0, before - limit), min(before, total)
                        elif frm is not None:
                            start, end = min(frm, total), min(frm + limit, total)
                        else:
                            start, end = max(0, total - limit), total
                        payload = {"id": sid, "size": size, "total": total,
                                   "start": start, "end": end,
                                   "has_more": start > 0 or st["base"] > 0,
                                   "shift": shift,
                                   "messages": st["msgs"][start:end]}
                    return self._raw(json.dumps(payload, ensure_ascii=False,
                                                separators=(",", ":")).encode())
                finally:
                    with mon.msg_locks_guard:
                        if mon.msg_active[cache_key] <= 1:
                            del mon.msg_active[cache_key]
                        else:
                            mon.msg_active[cache_key] -= 1
                        mon.msg_lru[cache_key] = None
                        mon.msg_lru.move_to_end(cache_key)
                        # 淘汰最久未用且无活动请求的缓存；全部都在活动中时允许暂时超限
                        while len(mon.msg_cache) > config.MSG_CACHE_SESSION_LIMIT and mon.msg_lru:
                            oldest = next(iter(mon.msg_lru))
                            if oldest in mon.msg_active:
                                break
                            mon.msg_lru.pop(oldest)
                            mon.msg_cache.pop(oldest, None)
                            mon.msg_locks.pop(oldest, None)
            if u.path.startswith("/sessions/"):
                sid = u.path.split("/", 2)[2]
                mon.ensure_subagent(sid)
                with mon.lock:
                    s = mon.by_sid(sid)
                    if not s:
                        return self._json({"error": "not found"}, 404)
                    d = s.detail(mon.stale_after, mon.project_of(s, mon.project_map()))
                mon.with_metadata(d)
                d["selected_model"] = mon.preferences.get(sid)
                d["approval"] = mon.approvals.get(sid)
                d["effort"] = mon.efforts.get(sid)
                d["interactions"] = mon.pending_interactions(sid)
                d["subagent_session_ids"] = mon.subagent_ids(s)
                h = mon.hosted_by_sid.get(sid)
                d["commands"] = list(h.commands) if h and h.alive() else []
                d["hosted"] = bool(h and h.alive())
                d["hosted_id"] = h.hid if h else None
                d["hosted_busy"] = bool(h and h.alive()
                                        and (h.current or h.pending or h.processing))
                d["hosted_queued"] = len(h.pending) if h and h.alive() else 0
                d["hosted_stream_text"] = h.stream_text if h and h.alive() else ""
                d["hosted_streaming"] = bool(h and h.alive() and h.stream_job is not None)
                return self._json(d)
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/commands":
                # 新建会话页的 slash 提示：进程未启动前先扫命令目录
                cwd = parse_qs(u.query).get("cwd", [""])[0]
                if not mon.known_project_cwd(cwd):
                    return self._json({"error": "目标目录不属于已监控项目"}, 400)
                return self._json({"commands": scan_slash_commands(config.CLAUDE_DIR, cwd)})
            if u.path == "/cc-switch":
                cwd = parse_qs(u.query).get("cwd", [""])[0]
                if not mon.known_project_cwd(cwd):
                    return self._json({"error": "目标目录不属于已监控项目"}, 400)
                try:
                    return self._json(mon.cc_switch.status(cwd))
                except SwitchError as e:
                    return self._json({"error": e.message}, e.code)
            if u.path == "/hosted":
                with mon.lock:
                    hosts = list(mon.hosted.values())
                return self._json([h.info() for h in hosts])
            if u.path.startswith("/jobs/"):
                job = mon.job_snapshot(u.path.split("/", 2)[2])
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

        def _safe_write_origin(self):
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

        def _safe_json_write(self):
            if not self._safe_write_origin():
                return False
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._json({"error": "Content-Type 必须为 application/json"}, 415)
                return False
            return True

        def _handle_hook(self, event):
            peer = self.client_address[0]
            if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return self._json({"error": "forbidden"}, 403)
            token = self.headers.get("X-Monitor-Token", "")
            if not mon.hook_token or not hmac.compare_digest(token, mon.hook_token):
                return self._json({"error": "unauthorized"}, 401)
            payload = self._read_body()
            if not isinstance(payload, dict):
                return self._json({})
            if event != "PreToolUse" or payload.get("tool_name") != "AskUserQuestion":
                return self._json({})
            sid = payload.get("session_id")
            tool_use_id = payload.get("tool_use_id")
            tool_input = payload.get("tool_input") or {}
            questions = tool_input.get("questions") or []
            key = f"{sid}\x00{tool_use_id}"
            with mon.lock:
                hosted = mon.hosted_by_sid.get(sid)
            with mon.lock:
                inter = mon.hook_interactions.get(key)
                if inter is None or inter.state != "pending":
                    inter = Interaction("hook-" + secrets.token_hex(8), hosted,
                                        {"tool_name": "AskUserQuestion",
                                         "tool_use_id": tool_use_id,
                                         "input": tool_input},
                                        session_id=sid, kind="choice",
                                        questions=questions)
                    mon.interactions[inter.id] = inter
                    mon.hook_interactions[key] = inter
                    emit_pending = True
                else:
                    emit_pending = False
            if emit_pending:
                with mon.lock:
                    sess = mon.by_sid(sid)
                if sess:
                    mon.emit(sess, "interaction_pending", inter.public())
            # 0 表示无限等待，与 stdio 审批路径（_await_interaction）语义一致
            timeout = mon.permission_timeout if mon.permission_timeout > 0 else None
            inter.event.wait(timeout)
            if inter.state == "pending":
                inter.resolve("deny", reason="审批超时，已自动跳过", state="timed_out")
            with mon.lock:
                mon.interactions.pop(inter.id, None)
                mon.hook_interactions.pop(key, None)
                sess = mon.by_sid(sid)
            if sess:
                mon.emit(sess, "interaction_resolved", inter.public())
            if inter.behavior == "allow" and inter.answers:
                updated = dict(tool_input)
                updated["answers"] = inter.answers
                return self._json({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "allow",
                    "permissionDecisionReason": "", "updatedInput": updated}})
            return self._json({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": inter.message or "User skipped the question"}})

        def do_POST(self):
            u = urlparse(self.path)
            print(f"[http] {self.command} {self.path} 收到", flush=True)   # 请求到达即记录，响应见 log_request
            if u.path.startswith("/hook/"):
                return self._handle_hook(u.path[len("/hook/"):])
            if not self._safe_json_write():
                return
            if u.path == "/system/power":
                body = self._read_body()
                action = body.get("action") if isinstance(body, dict) else None
                try:
                    system.power_command(action)   # 只为校验 action 合法性
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                try:
                    # spawn 即返回不等待命令完成；实际重启/挂起发生在响应写出之后
                    system.spawn_power(action)
                except OSError as e:
                    return self._json({"error": f"电源命令执行失败：{e}"}, 500)
                return self._json({"ok": True})
            if u.path == "/system/sleep-timer":
                body = self._read_body()
                raw = body.get("minutes") if isinstance(body, dict) else None
                # bool 是 int 子类要排除；Infinity/NaN 是 json.loads 接受的非标准字面量，
                # 不先 isfinite 拦下的话 int(raw) 会抛 OverflowError/ValueError 打崩请求线程
                valid = (not isinstance(raw, bool) and isinstance(raw, (int, float))
                         and math.isfinite(raw) and raw == int(raw) and 0 <= int(raw) <= 720)
                if not valid:
                    return self._json({"error": "minutes 必须是 0-720 之间的整数分钟（0 为取消）"}, 400)
                remaining = mon.set_sleep_timer(int(raw))
                return self._json({"ok": True, "remaining": remaining})
            if u.path == "/cc-switch/local":
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
                                              body.get("model", UNSET), body.get("approval"),
                                              body.get("effort", UNSET))
                return self._json(resp, code)
            if u.path.startswith("/interactions/") and u.path.endswith("/respond"):
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
            if len(parts) == 5 and parts[0] == "sessions" and parts[2] == "jobs" and parts[4] == "cancel":
                if self._read_body() != {}:
                    return self._json({"error": "body must be {}"}, 400)
                code, payload = mon.cancel_job(parts[1], parts[3])
                return self._json(payload, code)
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "metadata":
                try:
                    patch = validate_metadata_patch(self._read_body())
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                mon.ensure_subagent(parts[1])
                with mon.lock:
                    sess = mon.by_sid(parts[1])
                if not sess:
                    return self._json({"error": "session not found"}, 404)
                if sess.is_subagent:
                    return self._json({"error": "子代理会话只读，不能修改元数据"}, 400)
                try:
                    metadata = mon.metadata.update(parts[1], patch)
                except ValueError as e:
                    return self._json({"error": str(e)}, 500)
                mon.emit(sess, "session_metadata_changed", {"id": parts[1], **metadata})
                return self._json({"id": parts[1], **metadata})
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "model":
                body = self._read_body()
                if not isinstance(body, dict) or "model" not in body:
                    return self._json({"error": "body must contain model (string or null)"}, 400)
                mon.ensure_subagent(parts[1])
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
                code, resp = mon.interrupt_session(parts[1])
                return self._json(resp, code)
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "approval":
                body = self._read_body()
                mode = body.get("approval") if isinstance(body, dict) else None
                code, resp = mon.set_approval(parts[1], mode)
                return self._json(resp, code)
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "effort":
                body = self._read_body()
                level = body.get("effort") if isinstance(body, dict) else None
                code, resp = mon.set_effort(parts[1], level)
                return self._json(resp, code)
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
            if not self._safe_write_origin():
                return
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
            root = config.STATIC_DIR.resolve()
            fp = (root / name).resolve()
            if not path_is_within(fp, root) or not fp.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = {".js": "application/javascript", ".css": "text/css",
                     ".html": "text/html", ".map": "application/json"}.get(fp.suffix, "application/octet-stream")
            body = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _snapshot(self, only):
            # seq 先于数据采集分配：采集期间发出的事件 seq 都更大，
            # 客户端按 seq 去重时不会丢掉这些增量
            seq = mon.next_seq()
            if only:
                with mon.lock:
                    s = mon.by_sid(only)
                    detail = (s.detail(mon.stale_after, mon.project_of(s, mon.project_map()))
                              if s else None)
                mon.with_metadata(detail)
                h = mon.hosted_by_sid.get(only)
                data = {
                    "session": detail,
                    "interactions": mon.pending_interactions(only),
                    "live": {
                        "session_id": only,
                        "active": bool(h and h.alive()
                                       and (h.current or h.processing)),
                        "text": getattr(h, "stream_text", "") if h else "",
                    },
                }
            else:
                with mon.lock:
                    pmap = mon.project_map()
                    sessions = sorted(
                        (s.summary(mon.stale_after, mon.project_of(s, pmap))
                         for s in mon.unique_sessions()),
                        key=lambda x: x["last_input_at"] or x["last_event_at"],
                        reverse=True)
                    counts = Counter(i.session_id for i in mon.interactions.values()
                                     if i.state == "pending")
                for item in sessions:
                    mon.with_metadata(item)
                    item["pending_interactions"] = counts.get(item["id"], 0)
                    h = mon.hosted_by_sid.get(item["id"])
                    item["hosted"] = bool(h and h.alive())
                    item["hosted_id"] = h.hid if h else None
                data = {"sessions": sessions, "interactions": mon.pending_interactions()}
            return {"ts": util.now_ts(), "seq": seq, "session": only,
                    "type": "snapshot", "data": data}

        def _sse(self, only):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = queue.Queue(maxsize=1000)
            mon.subscribers.append(q)
            try:
                self.wfile.write(b"retry: 3000\n\n")
                snap = self._snapshot(only)
                self.wfile.write(
                    f"id: {snap['seq']}\nevent: snapshot\ndata: "
                    f"{json.dumps(snap, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                while True:
                    if getattr(q, "overflowed", False):
                        # 队列溢出说明已有事件被丢弃，继续发剩余事件会让客户端
                        # 误以为连续；断开让它重连并对账（EventSource 自动重连）
                        print("[sse] 订阅者积压溢出，断开连接强制全量对账", flush=True)
                        break
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        # 事件帧而非注释帧：前端看门狗靠它判断连接活性；
                        # 不带 id，SSE 游标保持粘性，重连对账不受影响
                        self.wfile.write(b"event: keepalive\ndata: {}\n\n")
                        self.wfile.flush()
                        continue
                    if only and ev["session"] != only:
                        continue
                    self.wfile.write(
                        f"id: {ev.get('seq', 0)}\nevent: {ev['type']}\ndata: "
                        f"{json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
            except OSError:
                # 客户端断开：Windows 上是 ConnectionAbortedError(10053)，
                # 其他平台多为 BrokenPipeError/ConnectionResetError
                pass
            finally:
                mon.subscribers.remove(q)

    return H
