"""Hosted：服务自己启动并长期持有的 claude 进程（stream-json 双向通信）。"""
import atexit
import json
import secrets
import subprocess
import threading
import time
import uuid
import weakref
from collections import deque

from session_preferences import (APPROVAL_MODES, EFFORT_LEVELS, default_model,
                                 effort_args, model_args, validate_model)

from . import config, util
from .util import clip, _arg_present, _arg_index, _merge_hook_settings
from .interactions import Interaction


# 解释器正常退出时兜底回收常驻进程；Windows 被强杀时不保证执行，
# 残留仍由 OrphanWatcher 观察。弱引用避免阻止 Hosted 被回收。
_LIVE_HOSTED = weakref.WeakSet()


def _terminate_process(proc, grace=5.0, lock=None):
    """先关 stdin 让 CLI 正常收尾，超时再 terminate/kill。

    grace 取 5 秒：与官方 SDK 一致，给 CLI 足够时间把最后一轮内容刷进转录。
    lock 为 Hosted.write_lock，避免关闭管道与发送线程并发写撞车。
    """
    if proc is None or proc.poll() is not None:
        return

    def close_stdin():
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except (OSError, ValueError):
            pass

    if lock is not None and lock.acquire(timeout=1.0):
        try:
            close_stdin()
        finally:
            lock.release()
    else:
        close_stdin()
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.terminate()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except (OSError, ValueError):
        pass


def _shutdown_hosted():
    for host in list(_LIVE_HOSTED):
        try:
            _terminate_process(getattr(host, "proc", None),
                               lock=getattr(host, "write_lock", None))
        except Exception:
            pass


atexit.register(_shutdown_hosted)


class Hosted:
    """服务自己启动并长期持有的 claude 进程（stream-json 双向通信）。

    消息到达后立即写进进程 stdin，不等待上一轮结束：CLI 自己维护命令队列，
    会在工具轮之间把新消息折叠进当前回合（absorbed_mid_turn），必要时还能
    抢占刚开始、尚未输出的回合。因此网页连续发送时不必等整轮跑完。
    `self.pending` 是“尚未终态”的消息（含已写入 CLI 队列的），
    `_claimed` 表示发送线程已领取、`_written` 表示已交给 CLI。
    会话照常写 ~/.claude/projects 的转录文件，所以原有的监听逻辑无需改动。
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
        # 已在本地收尾、随后可能仍会收到 command_lifecycle 的 uuid（中断/取消排队项）。
        # 只为过滤“外部回合”噪声，容量有限，收到对应终态帧即移除。
        self.resolved_commands = deque()
        self.resolved_command_set = set()
        self.max_resolved_commands = 128
        self.last_api_retry = None     # 最近一次上游重试，用于诊断 5xx/限流
        self.last_rate_limit = None    # 最近一次 rate_limit_event（status/resets_at/utilization）
        self.fast_mode = None          # fast 开关意图（服务侧记录，CLI 的 /fast 是会话级）
        self.fast_mode_state = None    # CLI 上报的 fast 状态（off/cooldown/on，见 _update_fast_mode）
        self.fast_mode_reason = None   # CLI 上报的不可用原因（sdk_opt_in_required 等，开放集合）
        self.last_conversation_reset = None  # /clear 等场景的 conversation_reset
        self.pending_conversation_reset = None  # 等下一帧 session_id 出现后迁移会话
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
            # --resume 的值是可选的，单 token 形式避免 dash 开头的值被当成新参数
            cmd += [f"--resume={self.resume_id}"]
        else:
            # 预生成 session id，使首轮开始前就能关联审批和事件
            if not self.session_id:
                self.session_id = str(uuid.uuid4())
            cmd += [f"--session-id={self.session_id}"]
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
                                     stderr=subprocess.PIPE, cwd=self.cwd,
                                     env=util.child_env(cwd=self.cwd))
        _LIVE_HOSTED.add(self)
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
            elif sum(1 for item in self.pending if item[1] is not self.current) >= self.max_queued:
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

    def _control(self, subtype, timeout=None, **params):
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
        if not event.wait(self.control_timeout if timeout is None else timeout):
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
            # rapidFollowupPreempt：回合还没输出内容时，新消息可让 CLI 直接接管
            # 当前回合（system/turn_preempted），网页端等待更短。
            try:
                self._control("initialize", hooks=None, rapidFollowupPreempt=True)
            except RuntimeError:
                # 旧版 CLI 明确拒绝未知字段时退回基础 initialize
                self._control("initialize", hooks=None)
            self.initialized = True
            while True:
                with self.ready:
                    item = self._next_unsent_locked()
                    while item is None and not self.stopping and self.status != "exited":
                        self.ready.wait()
                        item = self._next_unsent_locked()
                    if self.stopping or self.status == "exited":
                        break
                    text, job, model = item
                    target = (model if model is not None
                              else default_model(self.mon.claude_args))
                    # 忙时切模型会连带改变正在跑或排在 CLI 队列里的回合：这条消息要等
                    # 当前回合结束、且自己成为队首（前面的消息都已处理完）后再切。
                    # 模型相同的普通追发不受影响，仍然立即写入 CLI 队列。
                    if target != self.process_model and (
                            self.current is not None
                            or (self.pending and self.pending[0][1] is not job)):
                        self.ready.wait_for(
                            lambda: self.stopping or self.status == "exited"
                            or job.get("finished_at") is not None
                            or not self._pending_has_locked(job)
                            or (self.current is None and self.pending
                                and self.pending[0][1] is job))
                        continue
                    if job.get("finished_at") is not None or not self._pending_has_locked(job):
                        continue
                    # 领取即视为当前回合：cancel_job 对已领取项返回 409，
                    # 与旧的“popleft 即 running”语义一致。
                    job["_claimed"] = True
                    job["_writing"] = True
                    if self.current is None:
                        self.current = job
                        job["status"] = "running"
                        self.stream_text = ""
                        self.stream_job = job
                        self.last_delta_emit = 0.0
                    self.processing = True
                self.mon.queue_changed(job["session"])
                try:
                    target = model if model is not None else default_model(self.mon.claude_args)
                    if target != self.process_model:
                        self._control("set_model", model=target)
                        self.process_model = target
                    with self.ready:
                        # 领取后可能已被 interrupt/cancel 收尾，不能再写给 CLI
                        cancelled = (job.get("finished_at") is not None
                                     or not self._pending_has_locked(job)
                                     or self.interrupt_target is job or self.stopping
                                     or self.status == "exited")
                    if cancelled:
                        if job.get("finished_at") is None:
                            self._finish_job(job, "cancelled")
                        continue
                    self._write({"type": "user", "uuid": job.get("command_uuid"),
                                 "message": {"role": "user", "content": text}})
                    with self.ready:
                        job["_writing"] = False
                        job["_written"] = True
                        # 写入与 cancel/interrupt 撞车：job 已被收尾时把命令从 CLI 队列撤回
                        raced = job.get("finished_at") is not None
                    if raced:
                        try:
                            self._control("cancel_async_message", timeout=10,
                                          message_uuid=job.get("command_uuid"))
                        except (RuntimeError, TimeoutError, BrokenPipeError, OSError):
                            pass
                except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as e:
                    with self.ready:
                        self.mon._terminal_job_locked(job, "failed", error=str(e))
                        job["_writing"] = False
                        self.pending = deque(it for it in self.pending if it[1] is not job)
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
                # 不再等待上一轮结束：继续把后面的消息写进 CLI 队列
        except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as e:
            self.control_failed = True
            self.stopping = True
            print(f"[hosted] {self.hid} 控制失败: {e}", flush=True)
        finally:
            with self.ready:
                self.processing = False
                if self.control_failed:
                    for _, job, _ in self.pending:
                        self.mon._terminal_job_locked(
                            job, "failed", error="托管进程无法安全发送后续消息")
                    self.pending.clear()
                    self.current = None
                self.ready.notify_all()
            self.mon.queue_changed(self.session_id)
            if self.control_failed and self.proc and self.proc.poll() is None:
                self.proc.terminate()

    def _next_unsent_locked(self):
        """下一个还没领取的消息；调用者持有 ready。"""
        for item in self.pending:
            if not item[1].get("_claimed"):
                return item
        return None

    def _pending_has_locked(self, job):
        return any(item[1] is job for item in self.pending)

    def _job_by_uuid_locked(self, command_uuid):
        if not command_uuid:
            return None
        for _, job, _ in self.pending:
            if job.get("command_uuid") == command_uuid:
                return job
        return None

    def _remember_resolved_locked(self, job):
        """记录已在本地收尾的 command uuid，过滤随后到达的 lifecycle 噪声。"""
        cu = job.get("command_uuid")
        if not cu or cu in self.resolved_command_set:
            return
        self.resolved_command_set.add(cu)
        self.resolved_commands.append(cu)
        while len(self.resolved_commands) > self.max_resolved_commands:
            self.resolved_command_set.discard(self.resolved_commands.popleft())

    def _consume_resolved_locked(self, command_uuid):
        if not command_uuid or command_uuid not in self.resolved_command_set:
            return False
        self.resolved_command_set.discard(command_uuid)
        try:
            self.resolved_commands.remove(command_uuid)
        except ValueError:
            pass
        return True

    def queued_count(self):
        with self.ready:
            return sum(1 for _, job, _ in self.pending if job is not self.current)

    def _read_out(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):        # 跳过 [claude-code:...] 之类的非 JSON 行
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._maybe_switch_after_reset(obj)
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
                self._update_fast_mode(obj)
                self._on_init(obj.get("session_id"), obj)
            elif typ == "system" and obj.get("subtype") == "api_retry":
                retry = {"attempt": obj.get("attempt"),
                         "max_retries": obj.get("max_retries"),
                         "error_status": obj.get("error_status"),
                         "error": obj.get("error"),
                         "ts": util.now_ts()}
                with self.ready:
                    self.last_api_retry = retry
                # 事件里带上归一化后的 text/kind：网页无需再猜是网络、限流还是服务端
                self._emit_session_event("hosted_api_retry", {
                    "hosted": self.hid, **retry,
                    **util.api_error_fields({
                        "error": retry.get("error"),
                        "error_status": retry.get("error_status"),
                        "attempt": retry.get("attempt"),
                        "max_retries": retry.get("max_retries"),
                    })})
            elif typ == "system" and obj.get("subtype") == "turn_preempted":
                self._on_turn_preempted(obj)
            elif (config.DEBUG_TASK_FRAMES and typ == "system"
                  and obj.get("subtype") in (
                      "task_started", "task_progress", "task_updated",
                      "task_notification")):
                # 探针：确认 2.1.312 是否在 stdout 发后台任务帧。task_updated 的
                # 终态（killed/stopped）不一定伴随 task_notification，验证后再接 UI 收尾。
                patch = obj.get("patch") if isinstance(obj.get("patch"), dict) else {}
                print(f"[probe] {self.hid} system/{obj.get('subtype')} "
                      f"task={obj.get('task_id') or obj.get('tool_use_id')} "
                      f"status={obj.get('status') or patch.get('status')}", flush=True)
            elif typ == "rate_limit_event":
                self._on_rate_limit(obj)
            elif typ == "conversation_reset":
                self._on_conversation_reset(obj)
            elif typ == "command_lifecycle":
                self._on_lifecycle(obj)
            elif typ == "stream_event":
                self._on_stream_event(obj)
            elif typ == "assistant":
                # 完整 assistant 帧说明该段内容已成形；含工具调用时清掉流式预览，
                # 兼容不发 content_block_start 的旧版 CLI
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_use"
                        for b in content):
                    self._clear_stream_text()
            elif typ == "result":
                self._update_fast_mode(obj)
                self._on_result(obj)
            # 其余事件（thinking 等）转录文件里已有，这里不处理
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
            dead = []
            for job in ([self.current] if self.current else []) + [j for _, j, _ in self.pending]:
                if all(job is not seen for seen in dead):
                    dead.append(job)
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
        with self.ready:
            job = self._job_by_uuid_locked(cu)
            resolved = job is None and cu in self.resolved_command_set
            if resolved and state in self.LIFECYCLE_TERMINAL:
                self._consume_resolved_locked(cu)
        if resolved:
            # 已在本地取消/中断并收尾的消息，不再冒充“外部回合”
            return
        if state not in self.LIFECYCLE_TERMINAL:
            # queued / started / running / released ... 仍在这一轮内
            if job is not None and state in ("started", "running"):
                with self.ready:
                    if job.get("finished_at") is None:
                        job["status"] = "running"
                        if self.current is None:
                            # 上一轮已结束，CLI 开始处理队列里的下一条
                            self.current = job
                            self.processing = True
                            self.stream_text = ""
                            self.stream_job = job
                            self.last_delta_emit = 0.0
            return
        if job is None:
            self._emit_session_event("hosted_foreign_turn",
                                     {"hosted": self.hid, "state": state})
            return
        with self.ready:
            job["_terminal_state"] = state
            owner = job is self.current
        if state == "completed":
            if job.get("_result_seen"):
                self._finish_job(job, "completed", notify=owner)
            else:
                # result 可能稍后才到，留 3 秒收集回复内容
                threading.Timer(
                    3.0, lambda: self._finish_job(job, "completed", notify=owner)).start()
        else:
            self._finish_job(job, state, notify=owner)

    def _on_rate_limit(self, obj):
        info = obj.get("rate_limit_info")
        if not isinstance(info, dict):
            info = {}
        record = {
            "status": info.get("status"),
            "resets_at": info.get("resets_at"),
            "rate_limit_type": info.get("rate_limit_type"),
            "utilization": info.get("utilization"),
            "overage_status": info.get("overage_status"),
            "at": util.now_ts(),
        }
        with self.ready:
            self.last_rate_limit = record
        self._emit_session_event("hosted_rate_limit", {"hosted": self.hid, **record})
        print(f"[hosted] {self.hid} rate_limit status={record['status']} "
              f"resets_at={record['resets_at']}", flush=True)

    def _update_fast_mode(self, obj):
        """跟踪 CLI 在 system/init 与 result 帧上报的 fast 状态。

        state 是三值闭合集合（off/cooldown/on）；reason 是开放集合
        （sdk_opt_in_required / model_not_allowed / preference / …），未知值原样
        保留供前端展示。开关本身（self.fast_mode）由 set_fast 记录意图——
        state 混入了模型支持与账号可用性，不能当开关用（例如模型不支持时
        /fast on 成功但 state 仍为 off）。
        """
        if not isinstance(obj, dict) or "fast_mode_state" not in obj:
            return
        state = obj.get("fast_mode_state")
        state = state if state in ("off", "cooldown", "on") else None
        reason = obj.get("fast_mode_disabled_reason")
        reason = reason if isinstance(reason, str) else None
        with self.ready:
            changed = ((self.fast_mode_state, self.fast_mode_reason)
                       != (state, reason))
            self.fast_mode_state, self.fast_mode_reason = state, reason
        if changed:
            self._emit_session_event("hosted_fast_mode",
                                     {"hosted": self.hid,
                                      "fast_mode_state": state,
                                      "fast_mode_disabled_reason": reason})

    def _on_conversation_reset(self, obj):
        record = {"new_conversation_id": obj.get("new_conversation_id"),
                  "from": self.session_id,
                  "resume_from": self.resume_id,
                  "at": util.now_ts()}
        with self.ready:
            self.last_conversation_reset = record
            # 新会话的 session_id 不在 reset 帧里，等下一帧带 session_id 时迁移
            self.pending_conversation_reset = record
        self._emit_session_event("hosted_conversation_reset",
                                 {"hosted": self.hid, **record})
        print(f"[hosted] {self.hid} conversation_reset "
              f"new={record['new_conversation_id']}", flush=True)

    def _maybe_switch_after_reset(self, obj):
        """conversation_reset 之后，用下一帧真实的 session_id 完成会话迁移。"""
        with self.ready:
            pending = self.pending_conversation_reset
            if not pending:
                return
            if obj.get("parent_tool_use_id"):
                return
            sid = obj.get("session_id")
            if not isinstance(sid, str) or not sid or sid == self.session_id:
                return
            old, old_resume = self.session_id, self.resume_id
            self.pending_conversation_reset = None
            self.session_id = sid
            self.resume_id = sid
            self.last_conversation_reset = dict(
                self.last_conversation_reset or {}, session_id=sid)
        old_ids = {x for x in (old, old_resume) if x and x != sid}
        with self.mon.lock:
            old_sess = self.mon.by_sid(old)
            self.mon.hosted_by_sid[sid] = self
            for old_id in old_ids:
                if self.mon.hosted_by_sid.get(old_id) is self:
                    self.mon.hosted_by_sid.pop(old_id, None)
            for inter in self.mon.interactions.values():
                if inter.hosted_id == self.hid:
                    inter.session_id = sid
            for job in self.mon.jobs.values():
                if job.get("_owner") is self:
                    job["session"] = sid
        for binder, value in ((self.mon.preferences, self.selected_model),
                              (self.mon.efforts, self.effort),
                              (self.mon.approvals, self.approval)):
            if value is not None:
                try:
                    binder.bind(sid, value)
                except ValueError:
                    pass
        if old_sess:
            self.mon.emit(old_sess, "hosted_session_switched", {
                "hosted": self.hid, "from": old, "to": sid,
                "reason": "conversation_reset"})
        self.mon.queue_changed(old)
        self.mon.queue_changed(sid)
        print(f"[hosted] {self.hid} 会话已重置 {old} -> {sid}", flush=True)

    def _on_turn_preempted(self, obj):
        """CLI 为让新消息立刻执行而停掉了当前回合：标记被接管的 job，
        让随后到达的 cancelled 生命周期按“已被后续消息接管”收尾，而不是报中断失败。"""
        uuids = obj.get("preempted_message_uuids")
        if not isinstance(uuids, list):
            uuids = []
        with self.ready:
            for cu in uuids:
                job = self._job_by_uuid_locked(cu)
                if job is not None:
                    job["_preempted"] = True
            if self.current is not None:
                self.current["_preempted"] = True

    def _on_result(self, obj):
        uuid_list = obj.get("user_message_uuids")
        has_list = isinstance(uuid_list, list)
        single = obj.get("user_message_uuid")
        with self.ready:
            jobs = []
            if has_list:
                for cu in uuid_list:
                    job = self._job_by_uuid_locked(cu)
                    if job is not None and not any(job is seen for seen in jobs):
                        jobs.append(job)
            elif isinstance(single, str):
                job = self._job_by_uuid_locked(single)
                if job is not None:
                    jobs.append(job)
                if self.current is not None \
                        and not any(self.current is job for job in jobs):
                    # 批量消息的旧字段只带最后一个 uuid，当前回合（首条）也要收尾
                    jobs.insert(0, self.current)
            if not jobs:
                if has_list:
                    # 空 uuid 列表：明确没有可归属成员时只收当前回合，
                    # 不能把这一轮的结果错加给后面排队的新消息
                    if not uuid_list and self.current is not None:
                        jobs = [self.current]
                elif isinstance(single, str):
                    if self.current is not None:
                        jobs = [self.current]
                else:
                    # 极旧 CLI 的 result 完全不带 uuid：按提交顺序认领
                    # 下一个还没收到结果的 job（连续发送时不能只认 current）
                    nxt = next((j for _, j, _ in self.pending
                                if not j.get("_result_seen")), None)
                    if nxt is not None:
                        jobs = [nxt]
            owner = self.current if any(self.current is job for job in jobs) else None
            if owner is None and isinstance(single, str):
                owner = next((job for job in jobs
                              if job.get("command_uuid") == single), None)
            if owner is None and jobs:
                owner = jobs[-1]
            for job in jobs:
                job["result"] = obj.get("result")
                job["result_session_id"] = obj.get("session_id")
                job["cost_usd"] = obj.get("total_cost_usd")
                job["result_is_error"] = bool(obj.get("is_error"))
                job["terminal_reason"] = obj.get("terminal_reason")
                job["api_error_status"] = obj.get("api_error_status")
                job["_result_seen"] = True
                if obj.get("is_error"):
                    job["error"] = util.result_error_text(obj, "error")
            snapshot = [(job, job.get("_terminal_state"), job is owner,
                     bool(job.get("_preempted"))) for job in jobs]
        for job, terminal, is_owner, preempted in snapshot:
            if terminal:
                self._finish_job(job, terminal, notify=is_owner)
            else:
                # 被后续消息接管的回合即使只收到 result，也按取消路径收尾（显示为已接管）
                fallback = "cancelled" if preempted else "completed"
                threading.Timer(
                    3.0, lambda j=job, n=is_owner, s=fallback: self._finish_job(
                        j, s, notify=n)).start()

    def _finish_job(self, job, terminal_state, notify=True):
        with self.ready:
            if job.get("finished_at"):
                return
            if job.get("_preempted") and terminal_state in ("completed", "cancelled"):
                # 被后续消息接管：不是用户取消，也不是失败
                status, error = "done", None
                job["result"] = job.get("result") or "已被后续消息接管"
            elif terminal_state == "completed":
                status = "failed" if job.get("result_is_error") else "done"
                error = job.get("error")
            elif terminal_state == "cancelled":
                status, error = "cancelled", job.get("error") or "已中断"
            else:
                status, error = "failed", job.get("error") or f"command {terminal_state}"
            # fast 切换命令：正常跑完且 CLI 没有回 unavailable 才算执行成功
            self._settle_fast_intent_locked(
                job, executed=(status == "done" and not job.get("_preempted")
                               and not str(job.get("result") or "")
                               .startswith("Fast mode unavailable")))
            self.mon._terminal_job_locked(job, status, error=error)
            self.pending = deque(item for item in self.pending if item[1] is not job)
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
            if notify:
                self.turns += 1
        self.mon.queue_changed(job["session"])
        if not notify:
            return
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
            "is_error": job.get("result_is_error", False),
            "terminal_reason": job.get("terminal_reason"),
            "api_error_status": job.get("api_error_status")})

    def _on_stream_event(self, obj):
        event = obj.get("event") or {}
        etype = event.get("type")
        if etype == "content_block_start":
            # 模型开始调用工具：上一段流式文本已经作为 assistant 消息落盘，
            # 清掉临时气泡，否则网页会一直停在“助手正在输入…”的旧文本上
            if (event.get("content_block") or {}).get("type") == "tool_use":
                self._clear_stream_text()
            return
        if etype != "content_block_delta":
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

    def _clear_stream_text(self):
        """清空当前回合的流式预览（工具轮开始/回合收尾时调用）。"""
        with self.ready:
            job = self.current
            if job is None or self.stream_job is not job or not self.stream_text:
                return
            self.stream_text = ""
        self._emit_session_event("turn_delta", {
            "hosted": self.hid, "job": job.get("id"), "text": ""}, transient=True)

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
        # AskUserQuestion 的 can_use_tool 兜底必须真的等用户作答：full 的
        # “自动放行”只适用于权限类请求，替用户回答会把问题静默跳过。
        wait_for_user = self.approval != "full" or kind == "choice"
        with self.mon.lock:
            if request_id in self.mon.interactions:
                return
            if wait_for_user:
                self.mon.interactions[request_id] = inter
        if not wait_for_user:
            # 服务在微秒级自动放行，用户看不到审批卡：既广播事件会让网页对
            # 每次工具调用弹“待审批”通知并响铃（实测 pending→resolved 仅
            # 0.5ms），也不登记到 mon.interactions，避免快照恰好把它渲染成卡。
            inter.resolve("allow", "once", state="allowed")
            self._write_interaction(inter, emit=False)
        else:
            self._emit_session_event("interaction_pending", inter.public())
            threading.Thread(target=self._await_interaction, args=(inter,), daemon=True).start()

    def _await_interaction(self, inter):
        timeout = self.mon.permission_timeout
        inter.event.wait(timeout if timeout and timeout > 0 else None)
        if inter.state == "pending":
            inter.resolve("deny", reason="审批超时，已自动拒绝", state="timed_out")
        self._write_interaction(inter)

    def _write_interaction(self, inter, emit=True):
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
        if emit:
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

    def set_fast(self, on):
        """切换 fast mode（CLI 的 /fast，会话级）。返回 send 的 (code, resp)。

        开启前先用 apply_flag_settings 重新注入 flagSettings 的 fastMode：
        SDK（stream-json）模式默认 sdk_opt_in_required，且 /fast off 会把
        之前注入的 opt-in 一并清掉，所以每次开启都要先补一次（实测幂等）。
        关闭无需 opt-in，直接发命令。命令经正常消息通道执行，输出是命令
        气泡（"↯ Fast mode ON · model set to `Opus` …"），失败也能在对话里
        看到原因。注意 /fast on 可能把进程模型切到 Opus，process_model
        不会跟着变，后续带模型的发送仍按原记录切换。
        """
        on = bool(on)
        if not self.alive():
            return 409, {"error": "hosted process is not running"}
        if on:
            try:
                self._control("apply_flag_settings", settings={"fastMode": True})
            except (RuntimeError, TimeoutError) as e:
                return 409, {"error": f"fast mode opt-in 失败: {e}"}
        previous = self.fast_mode
        code, resp = self.send("/fast on" if on else "/fast off")
        if code == 202:
            self.fast_mode = on
            # 命令排队/执行期间可能被取消、被接管或被 CLI 拒绝（账号/模型不支持），
            # 这些路径实际没有切换成功，_finish_job 收尾时据此回滚意图
            job = self.mon.jobs.get(resp.get("job_id")) if resp.get("job_id") else None
            if job is not None:
                job["_fast_prev"] = previous
        return code, resp

    def _settle_fast_intent_locked(self, job, executed=False):
        """fast 切换命令的意图收尾：命令未真正执行时回滚开关记录。

        用键存在性判断标记（prev 的合法值含 None——切换前未设置过开关），
        不能拿值当哨兵。executed 为真的判据由调用方给（status=done 且非被
        接管且 CLI 未回复 "Fast mode unavailable"）；进程退出路径不调
        这里——Hosted 已死，fast 投影按 alive() 归 None。
        """
        if "_fast_prev" not in job:
            return
        prev = job.pop("_fast_prev")
        if not executed and self.fast_mode != prev:
            self.fast_mode = prev

    def cancel_queued(self, job, sid=None):
        """取消一条尚未开始执行的托管消息（可能已写进 CLI 的命令队列）。"""
        with self.ready:
            if sid is not None and job.get("session") != sid:
                return 404, {"error": "job not found"}
            if job.get("finished_at") is not None:
                return 200, self.mon._job_summary(job)
            if (job is self.current or job.get("_writing") or job.get("status") != "queued"
                    or not self._pending_has_locked(job)):
                return 409, {"error": "job is already claimed or finished"}
            written = bool(job.get("_written"))
        if written:
            # 已经交给 CLI，必须让它把命令从自己的队列里撤掉
            try:
                resp = self._control("cancel_async_message", timeout=10,
                                     message_uuid=job.get("command_uuid"))
            except (RuntimeError, TimeoutError, BrokenPipeError, OSError) as e:
                return 500, {"error": f"取消失败: {e}"}
            if not resp.get("cancelled"):
                return 409, {"error": "job is already claimed or finished"}
        with self.ready:
            if job.get("finished_at") is not None:
                return 200, self.mon._job_summary(job)
            if job is self.current or not self._pending_has_locked(job):
                return 409, {"error": "job is already claimed or finished"}
            # 取消排队项不是回合结束：不计轮数、不发 turn_done
            self._remember_resolved_locked(job)
            self._finish_job(job, "cancelled", notify=False)
            summary = self.mon._job_summary(job)
        self.mon.queue_changed(job["session"])
        return 200, summary

    def interrupt(self):
        if not self.alive():
            return 409, {"error": "hosted process is not running"}
        with self.ready:
            job = self.current
            # 当前回合走 CLI 的中断；其余尚未终态的消息（含已写入 CLI 队列的）直接取消
            queued = [j for _, j, _ in self.pending if j is not job]
            queued_count = len(queued)
            for queued_job in queued:
                self._settle_fast_intent_locked(queued_job)
                self._remember_resolved_locked(queued_job)
                self.mon._terminal_job_locked(queued_job, "cancelled", error="已中断")
            self.pending = deque(item for item in self.pending if item[1] is job) \
                if job else deque()
            self.interrupt_target = job
            self.interrupting = job is not None
            self.ready.notify_all()
        self.mon.queue_changed(self.session_id)
        try:
            self._write({"type": "control_request",
                         "request_id": f"monitor-int-{self.hid}-{int(time.time() * 1000)}",
                         "request": {"subtype": "interrupt", "cancel_queued": True}})
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
            # 当前回合仍让它正常收尾；其余（含已写入 CLI 队列但未开始的）直接作废
            for _, job, _ in self.pending:
                if job is not self.current:
                    self.mon._terminal_job_locked(
                        job, "failed", error="托管进程退出中，消息未发送")
            self.pending = deque(item for item in self.pending if item[1] is self.current)
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
            busy = current is not None or self.processing or bool(self.pending)
            queued = sum(1 for _, job, _ in self.pending if job is not current)
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
                "last_rate_limit": util.active_rate_limit(self.last_rate_limit),
                "fast_mode": self.fast_mode,
                "fast_mode_state": self.fast_mode_state,
                "fast_mode_reason": self.fast_mode_reason,
                "last_conversation_reset": self.last_conversation_reset,
                "needs_rehost": self.needs_rehost,
                "turns": turns, "returncode": self.returncode,
                "started_at": self.started_at, "stderr_tail": list(self.stderr_tail)}
