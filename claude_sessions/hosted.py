"""Hosted：服务自己启动并长期持有的 claude 进程（stream-json 双向通信）。"""
import json
import secrets
import subprocess
import threading
import time
import uuid
from collections import deque

from session_preferences import (APPROVAL_MODES, EFFORT_LEVELS, default_model,
                                 effort_args, model_args, validate_model)

from . import util
from .util import clip, _arg_present, _arg_index, _merge_hook_settings
from .interactions import Interaction


class Hosted:
    """服务自己启动并长期持有的 claude 进程（stream-json 双向通信）。

    消息写进同一个进程的 stdin；一轮结束（收到 result 事件）后再发下一条，
    模型忙的时候新消息先在这里排队。会话照常写 ~/.claude/projects 的转录文件，
    所以原有的监听逻辑无需改动。
    """

    def __init__(self, mon, cwd, resume_id=None, selected_model=None, approval=None,
                 effort=None):
        self.mon, self.cwd, self.resume_id = mon, cwd, resume_id
        self.hid = "h-" + secrets.token_hex(4)
        self.session_id = resume_id
        self.selected_model = selected_model
        self.effort = effort if effort in EFFORT_LEVELS else None
        self.process_effort = None    # 常驻进程当前档位（spawn 或在线切换后）
        self.commands = []            # system/init 广播的指令目录（去 / 前缀、排序）
        self.approval = approval or (mon.approvals.get(resume_id) if resume_id else "full")
        self.added_auto_permission = False   # 服务是否实际追加了 --permission-mode auto
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
        self.started_at = util.now_ts()
        self.stderr_tail = deque(maxlen=30)
        self.control_timeout = 60
        self.control_seq = 0
        self.control_waiters = {}
        self.initialized = False
        self.stopping = False
        self.control_failed = False
        self.needs_rehost = False
        self.interrupting = False
        self.interrupt_target = None   # 被中断的 job；只取消这一轮，不误伤后续消息
        self.lifecycle_seen = False
        self.last_api_retry = None     # 最近一次上游重试，用于诊断 5xx/限流
        self.stream_text = ""          # 当前 job 的流式文本聚合
        self.stream_job = None
        self.last_delta_emit = 0.0
        self.max_queued = 32

    def alive(self):
        return self.proc is not None and self.status == "running" and not self.stopping

    def _sess(self):
        with self.mon.lock:
            return self.mon.by_sid(self.session_id)

    def start(self, first_text):
        cmd = [self.mon.claude_bin, "-p", "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",
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
            self.added_auto_permission = True
        spawn_args = list(self.mon.claude_args)
        if self.mon.http_port and self.mon.hook_token:
            hook_settings = {"hooks": {"PreToolUse": [{
                "matcher": "AskUserQuestion",
                "hooks": [{"type": "command", "command": self.mon.hook_command(),
                           "timeout": 604800}]}]}}
            idx, current = _arg_index(spawn_args, "--settings")
            if idx is None:
                cmd += ["--settings", json.dumps(hook_settings, separators=(",", ":"))]
            elif current and current.lstrip().startswith("{"):
                try:
                    existing = json.loads(current)
                except json.JSONDecodeError:
                    existing = None
                merged = (_merge_hook_settings(existing, hook_settings)
                          if isinstance(existing, dict) else None)
                if merged is None:
                    print("[hosted] 已有 --settings 无法合并，跳过 AskUserQuestion hook 注册",
                          flush=True)
                else:
                    merged_json = json.dumps(merged, separators=(",", ":"))
                    if spawn_args[idx] == "--settings":
                        spawn_args[idx + 1] = merged_json
                    else:
                        spawn_args[idx] = "--settings=" + merged_json
            else:
                print("[hosted] --settings 指向文件，跳过 AskUserQuestion hook 注册", flush=True)
        cmd += effort_args(model_args(spawn_args, self.selected_model), self.effort)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=self.cwd)
        self.status = "running"
        self.process_effort = self.effort
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
        job = self.mon._new_job(self.session_id or self.hid, text, model, owner=self)
        with self.ready:
            job["command_uuid"] = str(uuid.uuid4())
            job["session"] = self.session_id or self.hid
            if self.status == "exited" or self.stopping or self.control_failed:
                self.mon._terminal_job_locked(job, "failed", error="hosted process is stopping")
                code, response = 409, {"error": job["error"]}
            elif len(self.pending) >= self.max_queued:
                self.mon._terminal_job_locked(job, "failed", error="hosted send queue is full")
                code, response = 429, {"error": "hosted send queue is full"}
            else:
                self.pending.append((text, job, model))
                self.ready.notify_all()
                code, response = 202, {"job_id": job["id"], "status": "queued", "hosted": self.hid}
        self.mon.queue_changed(job["session"])
        return code, response

    def _write(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except ValueError as e:
                # stop 关闭 stdin 后的并发写入抛 ValueError("write to closed file")，
                # 归入 BrokenPipe 路径，避免逃过 _send_loop 的异常处理
                raise BrokenPipeError(str(e)) from e

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
                    self.current = job
                    job["status"] = "running"
                    self.processing = True
                    self.stream_text = ""
                    self.stream_job = job
                    self.last_delta_emit = 0.0
                self.mon.queue_changed(job["session"])
                try:
                    target = model if model is not None else default_model(self.mon.claude_args)
                    if target != self.process_model:
                        self._control("set_model", model=target)
                        self.process_model = target
                    with self.ready:
                        cancelled = (self.interrupt_target is job or self.stopping
                                     or self.status == "exited")
                    if cancelled:
                        self._finish_job(job, "cancelled")
                        continue
                    self._write({"type": "user", "uuid": job.get("command_uuid"),
                                 "message": {"role": "user", "content": text}})
                except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as e:
                    with self.ready:
                        self.mon._terminal_job_locked(job, "failed", error=str(e))
                        if self.current is job:
                            self.current = None
                        self.processing = False
                        self.control_failed = isinstance(e, (TimeoutError, BrokenPipeError, OSError)) or "控制" in str(e)
                        if self.control_failed:
                            self.stopping = True
                        self.ready.notify_all()
                    self.mon.queue_changed(job["session"])
                    if self.control_failed:
                        break
                    continue
                with self.ready:
                    # 不要在 stopping 时清 current：让 result/lifecycle 或进程退出
                    # 给当前 job 收尾，否则它会永远停在 running
                    self.ready.wait_for(lambda: self.current is not job or self.stopping
                                        or self.status == "exited")
        except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as e:
            self.control_failed = True
            self.stopping = True
            print(f"[hosted] {self.hid} 控制失败: {e}", flush=True)
        finally:
            with self.ready:
                self.processing = False
                for _, job, _ in self.pending:
                    self.mon._terminal_job_locked(job, "failed", error="托管进程无法安全发送后续消息")
                self.pending.clear()
                self.ready.notify_all()
            self.mon.queue_changed(self.session_id)
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
                    inter.skip_response = True
                    inter.resolve("deny", reason="请求已被 Claude 取消", state="cancelled")
            elif typ == "system" and obj.get("subtype") == "init":
                self._on_init(obj.get("session_id"), obj)
            elif typ == "system" and obj.get("subtype") == "api_retry":
                retry = {"attempt": obj.get("attempt"),
                         "max_retries": obj.get("max_retries"),
                         "error_status": obj.get("error_status"),
                         "error": obj.get("error")}
                with self.ready:
                    self.last_api_retry = retry
                self._emit_session_event("hosted_api_retry",
                                         {"hosted": self.hid, **retry})
            elif typ == "command_lifecycle":
                self._on_lifecycle(obj)
            elif typ == "stream_event":
                self._on_stream_event(obj)
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
            self.interrupting = False
            self.interrupt_target = None
            self.stream_text = ""
            self.stream_job = None
            dead = ([self.current] if self.current else []) + [j for _, j, _ in self.pending]
            self.current = None
            self.pending.clear()
            tail = " | ".join(list(self.stderr_tail)[-3:])
            for waiter in self.control_waiters.values():
                waiter["error"] = "claude 进程已退出"
                waiter["event"].set()
            for job in dead:
                self.mon._terminal_job_locked(job, "failed",
                    error=job["error"] or f"claude 进程已退出 (code {rc}) {tail}")
            self.ready.notify_all()
        self.mon.queue_changed(self.session_id)
        print(f"[hosted] {self.hid} 退出 code={rc} 会话={self.session_id}"
              + (f" stderr: {tail}" if tail else ""), flush=True)
        # 进程已退出：唤醒所有等待审批的线程，避免永久阻塞
        with self.mon.lock:
            waiting = [i for i in self.mon.interactions.values() if i.hosted_id == self.hid]
        for inter in waiting:
            inter.skip_response = True
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

    def _on_init(self, sid, init_obj=None):
        if not sid:
            return
        names, seen = [], set()
        for item in (init_obj or {}).get("slash_commands") or []:
            if not isinstance(item, str):
                continue
            name = item.strip().lstrip("/")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        with self.ready:
            self.commands = sorted(names)
            old = self.session_id
            self.session_id = sid
            if old and old != sid and not self.resume_id:
                print(f"[hosted] {self.hid} session id 不一致: 预生成 {old}, init {sid}", flush=True)
            with self.mon.lock:
                self.mon.hosted_by_sid[sid] = self
                if self.resume_id:
                    self.mon.hosted_by_sid[self.resume_id] = self
                if old and old != sid:
                    # resume 会话即使 init 换了 sid（如 CLI 分配新 id）也保留原 id 的路由，
                    # 只清理新会话预生成 id 的映射
                    if old != self.resume_id and self.mon.hosted_by_sid.get(old) is self:
                        self.mon.hosted_by_sid.pop(old, None)
                    for inter in self.mon.interactions.values():
                        if inter.hosted_id == self.hid:
                            inter.session_id = sid
                for job in self.mon.jobs.values():
                    if job.get("_owner") is self:
                        job["session"] = sid
        if self.selected_model is not None:
            try:
                self.mon.preferences.bind(sid, self.selected_model)
            except ValueError:
                pass
        if self.effort is not None:
            try:
                self.mon.efforts.bind(sid, self.effort)
            except ValueError:
                pass

    def _emit_session_event(self, kind, data, transient=False):
        sess = self._sess()
        if sess:
            self.mon.emit(sess, kind, data, transient=transient)

    # CLI 会在回合过程中发送 queued/started/running 等非终态；
    # 只有以下状态才结束这一轮。
    LIFECYCLE_TERMINAL = {"completed", "cancelled", "discarded", "refused", "dropped"}

    def _on_lifecycle(self, obj):
        state = obj.get("state")
        cu = obj.get("command_uuid")
        self.lifecycle_seen = True
        if state not in self.LIFECYCLE_TERMINAL:
            # queued / started / running / released ... 仍在这一轮内
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
            self.mon._terminal_job_locked(job, status, error=error)
            if self.interrupt_target is job:
                self.interrupt_target = None
                self.interrupting = False
            if self.current is job:
                self.current = None
                self.processing = False
            final_text = ""
            if self.stream_job is job:
                final_text = self.stream_text
                self.stream_text = ""
                self.stream_job = None
            self.ready.notify_all()
            self.turns += 1
        self.mon.queue_changed(job["session"])
        print(f"[hosted] {self.hid} 第 {self.turns} 轮 {status} job={job.get('id')}"
              + (" 失败: " + error if error else ""), flush=True)
        if final_text:
            self._emit_session_event("turn_delta", {
                "hosted": self.hid, "job": job.get("id"), "text": final_text},
                transient=True)
        self._emit_session_event("hosted_turn_done", {
            "hosted": self.hid, "job": job.get("id"), "status": status,
            "result": clip(job.get("result") or "", 300),
            "cost_usd": job.get("cost_usd"),
            "result_session_id": job.get("result_session_id"),
            "duration": (max(0, job["finished_at"] - job["started_at"])
                         if job.get("finished_at") and job.get("started_at") else None),
            "is_error": job.get("result_is_error", False)})

    def _on_stream_event(self, obj):
        event = obj.get("event") or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta" or not delta.get("text"):
            return
        with self.ready:
            job = self.current
            if job is None or self.stream_job is not job:
                return
            self.stream_text += delta["text"]
            should_emit = util.now_ts() - self.last_delta_emit >= 0.08
            text = self.stream_text
            if should_emit:
                self.last_delta_emit = util.now_ts()
        if should_emit:
            self._emit_session_event("turn_delta", {
                "hosted": self.hid, "job": job.get("id"), "text": text},
                transient=True)

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
        request = obj.get("request") or {}
        kind = questions = None
        if request.get("tool_name") == "AskUserQuestion":
            # PreToolUse hook 未生效时的兜底路径：仍按问答卡呈现，
            # 网页才能逐题作答，而不是只能整卡放行/拒绝
            kind = "choice"
            questions = (request.get("input") or {}).get("questions") or []
        inter = Interaction(request_id, self, request,
                            kind=kind, questions=questions)
        with self.mon.lock:
            if request_id in self.mon.interactions:
                return
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
            updated_input = inter.updated_input
            if inter.answers:
                # choice 作答后把 answers 合入工具输入，CLI 才能拿到用户的回答
                updated_input = dict(updated_input)
                updated_input["answers"] = inter.answers
            response = {"behavior": "allow", "updatedInput": updated_input}
            if inter.scope == "always":
                response["updatedPermissions"] = inter.suggestions
        else:
            response = {"behavior": "deny", "message": inter.message or "用户拒绝"}
        payload = {"type": "control_response", "response": {
            "subtype": "success", "request_id": inter.id, "response": response}}
        try:
            if self.status != "exited" and not inter.skip_response:
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
        if mode == "auto":
            self._control("set_permission_mode", mode="auto")
            self.added_auto_permission = True
        elif self.approval == "auto" and self.added_auto_permission:
            # 只有服务把 CLI 切到过 auto 时才恢复 default；不要覆盖用户 --claude-args 的权限模式
            self._control("set_permission_mode", mode="default")
            self.added_auto_permission = False
        self.approval = mode

    def set_effort(self, level):
        """在线切换思考档位；None 回落模型默认。控制失败抛错，偏好由调用方处理。"""
        if level is not None and level not in EFFORT_LEVELS:
            raise ValueError("invalid effort level")
        if self.alive():
            self._control("apply_flag_settings", settings={"effortLevel": level})
            self.process_effort = level
        self.effort = level

    def interrupt(self):
        if not self.alive():
            return 409, {"error": "hosted process is not running"}
        with self.ready:
            queued_count = len(self.pending)
            for _, queued_job, _ in self.pending:
                self.mon._terminal_job_locked(queued_job, "cancelled", error="已中断")
            self.pending.clear()
            job = self.current
            self.interrupt_target = job
            self.interrupting = job is not None
            self.ready.notify_all()
        self.mon.queue_changed(self.session_id)
        try:
            self._write({"type": "control_request",
                         "request_id": f"monitor-int-{self.hid}-{int(time.time() * 1000)}",
                         "request": {"subtype": "interrupt"}})
        except (OSError, ValueError) as exc:
            with self.ready:
                self.interrupt_target = None
                self.interrupting = False
            if job:
                self._finish_job(job, "failed")
            return 500, {"error": f"中断写入失败: {exc}"}
        self._emit_session_event("turn_interrupted", {
            "hosted": self.hid, "job": job.get("id") if job else None,
            "queued_cancelled": queued_count})
        with self.mon.lock:
            waiting = [i for i in self.mon.interactions.values()
                       if i.hosted_id == self.hid]
        for inter in waiting:
            inter.skip_response = True
            inter.resolve("deny", reason="回合已中断", state="cancelled")
        if job:
            threading.Timer(30.0, lambda: self._finish_job(job, "cancelled")).start()
        return 200, {"interrupted": True, "queued_cancelled": True}

    def stop(self, kill=False):
        if self.proc is None or self.status == "exited":
            return
        with self.mon.lock:
            waiting = [i for i in self.mon.interactions.values()
                       if i.hosted_id == self.hid]
        for inter in waiting:
            inter.skip_response = True
            inter.resolve("deny", reason="托管进程正在退出", state="cancelled")
        with self.ready:
            self.stopping = True
            self.ready.notify_all()
            for _, job, _ in self.pending:
                self.mon._terminal_job_locked(job, "failed", error="托管进程退出中，消息未发送")
            self.pending.clear()
        self.mon.queue_changed(self.session_id)
        try:
            if kill:
                self.proc.terminate()
            else:
                # 关闭 stdin = 让它做完当前一轮后正常退出；与 _write 共用
                # write_lock，避免并发写入撞上刚关闭的管道
                with self.write_lock:
                    self.proc.stdin.close()
                self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
        except Exception:
            pass

    def info(self):
        with self.ready:
            current = self.current
            busy = current is not None or self.processing
            queued = len(self.pending)
            approval = self.approval
            interrupting = self.interrupting
            turns = self.turns
            stream_text = self.stream_text
            streaming = self.stream_job is not None
        return {"hosted_id": self.hid, "session_id": self.session_id, "cwd": self.cwd,
                "status": self.status, "pid": self.proc.pid if self.proc else None,
                "busy": busy, "queued": queued,
                "approval": approval, "interrupting": interrupting,
                "current_job": current.get("id") if current else None,
                "stream_text": stream_text, "streaming": streaming,
                "last_api_retry": self.last_api_retry,
                "needs_rehost": self.needs_rehost,
                "turns": turns, "returncode": self.returncode,
                "started_at": self.started_at, "stderr_tail": list(self.stderr_tail)}
