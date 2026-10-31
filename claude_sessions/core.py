"""Monitor：会话注册表、轮询扫描、远程消息/队列、审批协调与事件分发。

以文件路径为键管理 Session；Hosted 常驻进程、HTTP 层都以它为协调中枢。
可替换依赖（config 常量、system 电源操作、util.now_ts 时钟）一律模块属性访问。
"""
import base64
import hashlib
import json
import math
import os
import queue
import secrets
import shlex
import subprocess
import sys
import threading
import urllib.request
from collections import Counter, OrderedDict, deque
from pathlib import Path

from scan_cache import ScanCache
from session_preferences import (APPROVAL_MODES, ApprovalPreferences, EFFORT_LEVELS,
                                 EffortPreferences, ModelPreferences,
                                 SessionMetadata, UNSET, default_model, effort_args,
                                 model_args, validate_effort, validate_model)
from cc_switch import CCSwitch

from . import config, system, util
from .util import clip
from .session import Session
from .transcript import classify_user_line, TASK_TOOL_USE_ID_RE, _result_text
from .hosted import Hosted
from .orphans import OrphanWatcher


class Monitor:
    def __init__(self, stale_after, webhook=None, permission_timeout=300):
        # 以文件路径为键：同一 session id 可能出现在多个项目目录（恢复/移动过），
        # 若按 id 键控，两个文件的读取 offset 会互相干扰导致重复计数
        self.sessions = {}           # str(path) -> Session
        self.stale_after = stale_after
        self.webhook = webhook
        # webhook 单 worker 顺序发送：不再每个事件起线程，突发时线程数不受控
        self.webhook_queue = queue.Queue(maxsize=config.WEBHOOK_QUEUE_LIMIT) if webhook else None
        self.webhook_dropped = 0
        self.webhook_dropped_logged = 0.0
        if webhook:
            threading.Thread(target=self._webhook_worker, daemon=True).start()
        self.permission_timeout = permission_timeout
        self.interactions = {}       # request_id -> Interaction
        self.hook_interactions = {}  # session+tool_use -> Interaction（AskUserQuestion 去重）
        self.http_port = 0
        self.hook_token = ""
        self.event_seq = 0
        self.event_lock = threading.Lock()
        self.msg_locks = {}
        self.msg_locks_guard = threading.Lock()
        # 每个转录路径一把扫描锁：同一文件的读取与 offset 提交串行化，
        # 但磁盘 I/O 不再占住 mon.lock（慢盘不阻塞 HTTP 与审批）
        self.scan_locks = {}
        self.scan_locks_guard = threading.Lock()
        self.lock = threading.Lock()
        self.subscribers = []      # list of queue.Queue
        self.first_scan_done = False
        self.msg_cache = {}        # str(path) -> {base, off, buf, msgs, tools} 对话增量缓存
        self.msg_lru = OrderedDict()   # 最近使用的缓存键；末尾为最新，头部优先淘汰
        self.msg_active = Counter()    # 缓存键 -> 正在处理的请求数；活动中的缓存不淘汰
        # 远程发消息 / 托管相关
        self.claude_bin = "claude"
        self.claude_args = []
        self.msg_timeout = 600
        self.jobs = {}             # 注册表归 lock；字段归 job._owner.ready 或 lock
        self.terminal_jobs = deque()  # 首次终态完成顺序，全局保留 100 条
        self.busy = set()          # 正在执行远程消息的 session id
        self.remote_queues = {}    # sid -> deque[(job, text, model)]，非托管一次性消息排队
        self.new_cwd_busy = set()  # 尚未取得 session id 的新会话目录
        self.hosted = {}           # hosted_id -> Hosted（服务托管的常驻进程）
        self.hosted_by_sid = {}    # session id -> Hosted
        self.preferences = ModelPreferences(config.CLAUDE_DIR / "claude-sessions" / "preferences.json")
        self.approvals = ApprovalPreferences(config.CLAUDE_DIR / "claude-sessions" / "approvals.json")
        self.efforts = EffortPreferences(config.CLAUDE_DIR / "claude-sessions" / "efforts.json")
        self.metadata = SessionMetadata(config.CLAUDE_DIR / "claude-sessions" / "session-metadata.json")
        self.cc_switch = CCSwitch(config.CLAUDE_DIR)
        # 子代理转录默认不扫描；需要时通过 --scan-subagents 或 include_subagents=1 加载
        self.scan_subagents = False
        self.subagent_scan_lock = threading.Lock()
        self.subagents_loaded_at = 0.0
        # 扫描缓存延迟初始化：只调用 handle_line 的测试不会读写真实缓存文件
        self.scan_cache = None
        self.scan_cache_entries = {}
        self.cache_dirty = False
        self.last_cache_save = 0.0
        self.shutdown_event = threading.Event()
        # 定时睡眠：内存态 threading.Timer，服务重启不恢复
        self.sleep_timer_lock = threading.Lock()
        self.sleep_timer = None
        self.sleep_timer_deadline = None
        # 干活时推迟睡眠：仅跟随 working 状态持有，进程退出自动失效；
        # 开关本身持久化，默认关
        self.keep_awake = self._load_keep_awake_pref()
        self.awake_held = False
        self.last_awake_check = 0.0
        # 孤儿进程观察：托管退出后仍存活的后代进程
        self.orphans = OrphanWatcher()
        self.last_orphan_sample = 0.0

    def _keep_awake_pref_path(self):
        return config.CLAUDE_DIR / "claude-sessions" / "power.json"

    def _load_keep_awake_pref(self):
        try:
            data = json.loads(self._keep_awake_pref_path().read_text(encoding="utf-8"))
            return bool(data.get("keep_awake"))
        except (OSError, ValueError):
            return False

    def set_keep_awake(self, enabled):
        """开关「有会话工作时阻止系统自动睡眠」；立即按当前状态评估一次。"""
        if enabled and not system.keep_awake_supported():
            return 400, {"error": "当前平台不支持推迟睡眠"}
        self.keep_awake = bool(enabled)
        path = self._keep_awake_pref_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"keep_awake": self.keep_awake}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            print(f"[system] 保存推迟睡眠开关失败: {e}", flush=True)
        self._apply_keep_awake()
        return 200, {"keep_awake": self.keep_awake, "awake_held": self.awake_held}

    def _keep_awake_needed(self):
        if not self.keep_awake:
            return False
        now = util.now_ts()
        with self.lock:
            return any(s.state == "working" and s.last_event_ts
                       and now - s.last_event_ts <= self.stale_after
                       for s in self.unique_sessions())

    def _apply_keep_awake(self):
        needed = self._keep_awake_needed()
        if needed == self.awake_held:
            return
        try:
            system.set_keep_awake(needed)
            self.awake_held = needed
            print(f"[system] 推迟睡眠 {'开启（有会话工作中）' if needed else '解除'}", flush=True)
        except (OSError, system.SystemInfoUnavailable) as e:
            print(f"[system] 推迟睡眠设置失败: {e}", flush=True)

    def with_metadata(self, summary):
        """仅装饰公开投影，不修改 Session/title/扫描缓存。"""
        if summary is not None:
            summary.update(self.metadata.get(summary["id"]))
        return summary

    def set_sleep_timer(self, minutes):
        """设定/替换定时睡眠（minutes 分钟后执行 sleep 动作）；0 取消。
        返回剩余秒数，无定时时为 None。"""
        with self.sleep_timer_lock:
            if self.sleep_timer is not None:
                self.sleep_timer.cancel()
            self.sleep_timer = None
            self.sleep_timer_deadline = None
            if minutes:
                self.sleep_timer_deadline = util.now_ts() + minutes * 60
                self.sleep_timer = threading.Timer(minutes * 60, self._fire_sleep_timer)
                self.sleep_timer.daemon = True
                self.sleep_timer.start()
        return self.sleep_timer_remaining()

    def sleep_timer_remaining(self):
        """剩余秒数（向上取整），无定时时为 None。"""
        with self.sleep_timer_lock:
            if self.sleep_timer_deadline is None:
                return None
            return max(0, math.ceil(self.sleep_timer_deadline - util.now_ts()))

    def _fire_sleep_timer(self):
        # 先清状态再执行：睡醒后查询不会看到残留定时；执行失败只记日志（Timer 线程无人接异常）。
        # 身份校验：fire 与 set 锁窗口竞态时（旧 Timer 的 cancel 已来不及），这里自己已被
        # 新定时替换，作废本轮——不清掉新装的状态，也不二次睡眠
        with self.sleep_timer_lock:
            if self.sleep_timer is not threading.current_thread():
                return
            self.sleep_timer_deadline = None
            self.sleep_timer = None
        try:
            system.spawn_power("sleep")
        except (OSError, ValueError) as e:
            print(f"[system] 定时睡眠执行失败：{e}", flush=True)

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
        cwds = {util.project_root(s.cwd) for s in self.sessions.values() if s.cwd}
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
        return pmap.get(util.project_root(sess.cwd))

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
            known.update(Path(util.project_root(s.cwd)).expanduser().resolve()
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
        # 子代理默认懒加载；这里补加载以保留“只读、拒绝发送”的 400 语义
        self.ensure_subagent(sid)
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
        try:
            if model is not UNSET:
                self.preferences.set(sid, selected)
        except ValueError as e:
            return 500, {"error": str(e)}
        job = self._new_job(sid, text, selected)
        with self.lock:
            queued = sid in self.busy
            if queued and len(self.remote_queues.get(sid, ())) >= config.REMOTE_QUEUE_LIMIT:
                error, code = "remote message queue is full", 429
            elif not queued and sess.summary(self.stale_after)["state"] == "working" and not force:
                error, code = "session is working; retry when idle, or pass force=true", 409
            else:
                error = None
            if error:
                self._terminal_job_locked(job, "failed", error=error)
            elif queued:
                self.remote_queues.setdefault(sid, deque()).append((job, text, selected))
            else:
                self.busy.add(sid)
                job["status"] = "running"
            status = job["status"]
        self.queue_changed(sid)
        if error:
            return code, {"error": error}
        if not queued:
            threading.Thread(target=self._run_job, args=(sess, job, text, selected), daemon=True).start()
        return 202, {"job_id": job["id"], "status": status}

    def _run_job(self, sess, job, text, selected_model=None):
        self.emit(sess, "remote_message_sent", {"job": job["id"], "text": job["text"]})
        cmd = [self.claude_bin, "-p", "--resume", sess.id, "--output-format", "json"]
        cmd += model_args(self.claude_args, selected_model)
        print(f"[msg] job {job['id']} 会话 {sess.id} 开始: {job['text']}", flush=True)
        self._exec(job, cmd, sess.cwd, text)
        with self.lock:
            completed = self._job_public(job)
        print(f"[msg] job {completed['id']} {completed['status']}"
              + (f": {completed['error']}" if completed["error"] else f" {clip(completed['result'] or '', 120)}"), flush=True)
        self.emit(sess, "remote_message_done", {"job": completed["id"], "status": completed["status"],
                                                "error": completed["error"],
                                                "result": clip(completed["result"] or "", 300)})
        next_item = None
        with self.lock:
            pending = self.remote_queues.get(sess.id)
            if pending:
                next_item = pending.popleft()
                next_item[0]["status"] = "running"
                if not pending:
                    self.remote_queues.pop(sess.id, None)
            else:
                self.busy.discard(sess.id)
        with self.lock:
            if job.get("finished_at") is not None:
                self._register_terminal_locked(job)  # 兼容替换 _exec 的本地执行器
        self.queue_changed(sess.id)
        if next_item:
            next_job, next_text, next_model = next_item
            threading.Thread(target=self._run_job,
                             args=(sess, next_job, next_text, next_model), daemon=True).start()

    def _exec(self, job, cmd, cwd, text):
        """同步跑一轮；结果先存局部，最终在 owner 锁下一次提交。"""
        result, result_sid, error = None, None, None
        try:
            # 环境变量原样继承，所以 ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY 等自定义配置同样生效
            # 显式 utf-8：Windows 中文系统默认是 gbk，会把中文消息和回复弄乱
            p = subprocess.run(cmd, input=text, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=cwd, timeout=self.msg_timeout)
            out = p.stdout.strip()
            try:
                data = json.loads(out)
                result = data.get("result")
                result_sid = data.get("session_id")
                if data.get("is_error") or p.returncode != 0:
                    error = clip(data.get("result") or p.stderr, 500)
            except json.JSONDecodeError:
                result = clip(out, 2000)
                if p.returncode != 0:
                    error = clip(p.stderr or out, 500)
        except FileNotFoundError:
            error = f"找不到命令 {self.claude_bin!r}，请用 --claude-bin 指定路径"
        except subprocess.TimeoutExpired:
            error = f"超时（{self.msg_timeout}s）"
        except Exception as e:
            error = str(e)
        finally:
            with self.lock:
                self._terminal_job_locked(job, "failed" if error else "done",
                                          result=result, result_session_id=result_sid, error=error,
                                          session=job["session"] or result_sid)
                sid = job["session"]
            self.queue_changed(sid)

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
            with self.lock:
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
                    with self.lock:
                        job["preference_error"] = str(e)
            print(f"[msg] job {job['id']} {job['status']} 新会话={sid}"
                  + (f": {job['error']}" if job["error"] else ""), flush=True)
        finally:
            if cwd_key:
                with self.lock:
                    self.new_cwd_busy.discard(cwd_key)

    def _new_job(self, sid, text, selected_model=None, owner=None):
        job = {"id": secrets.token_hex(6), "session": sid, "status": "queued",
               "started_at": util.now_ts(), "finished_at": None, "text": clip(text, 200),
               "requested_model": selected_model, "_owner": owner,
               "result": None, "error": None, "result_session_id": None}
        with self.lock:
            self.jobs[job["id"]] = job
        return job

    def _terminal_job_locked(self, job, status, **fields):
        """调用者持有字段 owner 锁；唯一允许的嵌套方向为 ready → lock。"""
        if job.get("finished_at") is not None:
            return False
        job.update(fields, status=status, finished_at=util.now_ts())
        self._register_terminal_locked(job)
        return True

    def _register_terminal_locked(self, job):
        def register():
            if job.get("_terminal_registered"):
                return
            job["_terminal_registered"] = True
            self.terminal_jobs.append(job["id"])
            while len(self.terminal_jobs) > 100:
                self.jobs.pop(self.terminal_jobs.popleft(), None)
        if job.get("_owner") is None:
            register()  # 非托管调用者已持有 Monitor.lock（不可重入）
        else:
            with self.lock:
                register()

    @staticmethod
    def _job_public(job):
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def job_snapshot(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
        if job is None:
            return None
        owner = job.get("_owner")
        with owner.ready if owner else self.lock:
            return self._job_public(job)

    @staticmethod
    def _job_summary(job, position=None):
        return {"id": job["id"], "status": job["status"], "text": job["text"],
                "requested_model": job["requested_model"], "created_at": job["started_at"],
                "finished_at": job["finished_at"], "error": job["error"],
                "cancellable": position is not None and job["status"] == "queued",
                "position": position}

    def session_jobs(self, sid):
        # 仅注册表引用在 lock 下取得；各 owner 的字段/队列在同一锁下投影。
        with self.lock:
            refs = list(self.jobs.values())
            known = self.by_sid(sid) is not None or sid in self.hosted_by_sid
        groups = {}
        for job in refs:
            groups.setdefault(job.get("_owner"), []).append(job)
        result = {"session": sid, "running": [], "queued": [], "recent": []}
        for owner, jobs in groups.items():
            with owner.ready if owner else self.lock:
                pending = owner.pending if owner else self.remote_queues.get(sid, ())
                positions = {item[1 if owner else 0]["id"]: n
                             for n, item in enumerate(pending, 1)}
                for job in jobs:
                    if job["session"] != sid:
                        continue
                    known = True
                    bucket = ("recent" if job["finished_at"] is not None else
                              "running" if job["status"] == "running" else "queued")
                    result[bucket].append(self._job_summary(job, positions.get(job["id"])))
        result["queued"].sort(key=lambda j: (j["position"] is None, j["position"] or 0, j["created_at"]))
        result["recent"].sort(key=lambda j: j["finished_at"], reverse=True)
        result["recent"] = result["recent"][:20]
        return (200, result) if known else (404, {"error": "session not found"})

    def cancel_job(self, sid, jid):
        self.ensure_subagent(sid)
        with self.lock:
            sess = self.by_sid(sid)
            job = self.jobs.get(jid)
        if sess and sess.is_subagent:
            return 400, {"error": "子代理会话只读，不能取消消息"}
        if job is None:
            return 404, {"error": "job not found"}
        owner = job.get("_owner")
        with owner.ready if owner else self.lock:
            if job["session"] != sid:
                return 404, {"error": "job not found"}
            if job["status"] == "cancelled":
                return 200, self._job_summary(job)
            pending = owner.pending if owner else self.remote_queues.get(sid, ())
            item = next((item for item in pending if item[1 if owner else 0] is job), None)
            if job["status"] != "queued" or item is None:
                return 409, {"error": "job is already claimed or finished"}
            pending.remove(item)
            if not owner and not pending:
                self.remote_queues.pop(sid, None)
            self._terminal_job_locked(job, "cancelled")
            summary = self._job_summary(job)
        self.queue_changed(sid)
        return 200, summary

    def queue_changed(self, sid):
        with self.lock:
            sess = self.by_sid(sid)
        if sess:
            self.emit(sess, "queue_changed", {"session": sid})

    def host_session(self, cwd, resume_id, text, force=False, model=UNSET,
                     approval=None, effort=UNSET):
        # 子代理默认懒加载；补加载以保留“只读、拒绝托管”的 400 语义
        if resume_id:
            self.ensure_subagent(resume_id)
        with self.lock:
            sess = self.by_sid(resume_id) if resume_id else None
        target = cwd or (sess.cwd if sess else None)
        if not isinstance(target, str) or not target or not os.path.isdir(target):
            return 400, {"error": f"cwd unavailable: {target}"}
        with self.cc_switch.operation(target):
            return self._host_session_locked(cwd, resume_id, text, force, model,
                                             approval, effort)

    def _host_session_locked(self, cwd, resume_id, text, force=False, model=UNSET,
                             approval=None, effort=UNSET):
        """启动并托管常驻进程；resume_id 为空则新建会话。"""
        if approval is not None and approval not in APPROVAL_MODES:
            return 400, {"error": "approval 必须是 ask / auto / full"}
        try:
            selected = validate_model(self.preferences.get(resume_id) if model is UNSET and resume_id
                                     else None if model is UNSET else model)
        except ValueError as e:
            return 400, {"error": str(e)}
        if effort is not UNSET:
            try:
                selected_effort = validate_effort(effort)
            except ValueError as e:
                return 400, {"error": str(e)}
        else:
            selected_effort = self.efforts.get(resume_id) if resume_id else None
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
        if effort is not UNSET and resume_id:
            try:
                self.efforts.set(resume_id, selected_effort)
            except ValueError as e:
                return 500, {"error": str(e)}
        h = Hosted(self, cwd, resume_id, selected,
                   approval=approval or (self.approvals.get(resume_id) if resume_id else "full"),
                   effort=selected_effort)
        try:
            code, resp = h.start(text)
        except FileNotFoundError:
            return 500, {"error": f"找不到命令 {self.claude_bin!r}，请用 --claude-bin 指定路径"}
        except Exception as e:
            return 500, {"error": str(e)}
        with self.lock:
            self.hosted[h.hid] = h
            # 新会话预生成的 id 也要立刻可路由，避免 init 到达前的竞态
            if h.session_id:
                self.hosted_by_sid[h.session_id] = h
            if resume_id:
                self.hosted_by_sid[resume_id] = h
        resp["hosted_id"] = h.hid
        print(f"[hosted] {h.hid} 启动 (cwd={cwd}, resume={resume_id or '新会话'})", flush=True)
        return code, resp

    # ---------- 权限审批 ----------
    def hook_command(self):
        # hook 命令必须指向项目根的入口 monitor.py（本文件在包内，__file__ 不是入口）
        entry = Path(__file__).resolve().parent.parent / "monitor.py"
        args = [sys.executable, str(entry), "hook",
                "--port", str(self.http_port), "--token", self.hook_token,
                # 0 传给客户端表示无限等待，与服务端审批语义一致
                "--timeout", str(self.permission_timeout)]
        if os.name == "nt":
            # Claude Code 在 Windows 上经 Git Bash 执行 hook，反斜杠路径会被 bash
            # 当转义吃掉（F:\code\... → F:code... → exit 127），必须换成正斜杠
            return subprocess.list2cmdline(args).replace("\\", "/")
        return shlex.join(args)

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

    def set_approval(self, sid, mode):
        """先切换活动托管进程，成功后再持久化，避免失败时磁盘与内存不一致。"""
        if mode not in APPROVAL_MODES:
            return 400, {"error": "approval 必须是 ask / auto / full"}
        with self.lock:
            h = self.hosted_by_sid.get(sid)
        if h and h.alive():
            try:
                h.set_approval(mode)
            except (RuntimeError, TimeoutError) as e:
                return 409, {"error": f"实时切换失败: {e}"}
        try:
            self.approvals.set(sid, mode)
        except ValueError as e:
            return 500, {"error": str(e)}
        return 200, {"approval": mode}

    def set_effort(self, sid, level):
        """保存思考档位；托管中先在线切换，失败不回滚保存（重托管后生效）。"""
        try:
            normalized = validate_effort(level)
        except ValueError as e:
            return 400, {"error": str(e)}
        with self.lock:
            h = self.hosted_by_sid.get(sid)
        warning = None
        if h and h.alive():
            try:
                h.set_effort(normalized)
            except (RuntimeError, TimeoutError, ValueError) as e:
                warning = f"在线切换失败（已保存，重托管后生效）: {e}"
        try:
            self.efforts.set(sid, normalized)
        except ValueError as e:
            return 500, {"error": str(e)}
        resp = {"effort": normalized or "default"}
        if warning:
            resp["warning"] = warning
        return 200, resp

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
            if inter.kind == "choice" and behavior == "allow":
                answers = body.get("answers")
                if not isinstance(answers, dict):
                    return 400, {"error": "choice 必须提供 answers"}
                normalized = {}
                for question in inter.questions:
                    if not isinstance(question, dict) \
                            or not isinstance(question.get("question"), str):
                        return 400, {"error": "问题格式无效"}
                    text = question["question"]
                    answer = answers.get(text)
                    if not isinstance(answer, str) or not answer.strip():
                        return 400, {"error": f"缺少问题答案: {text}"}
                    normalized[text] = answer.strip()
                inter.answers = normalized
            reason = body.get("reason")
            reason = clip(reason, 500) if isinstance(reason, str) else None
            if not inter.resolve(behavior, scope, reason):
                return 409, {"error": "interaction already resolved"}
        return 200, inter.public()

    # ---------- 事件分发 ----------
    def next_seq(self):
        with self.event_lock:
            self.event_seq += 1
            return self.event_seq

    def emit(self, sess, kind, data, transient=False):
        if not self.first_scan_done:   # 首次扫描只回放历史状态，不推送
            return
        ev = {"ts": util.now_ts(), "seq": self.next_seq(),
              "session": sess.id if sess else None, "type": kind, "data": data}
        if not transient and sess is not None:
            sess.recent.append(ev)
        for q in list(self.subscribers):
            try:
                q.put_nowait(ev)
            except queue.Full:
                # 不再静默丢事件：客户端无法察觉缺口，看板会悄悄过期。
                # 标记溢出让 SSE 循环断开连接，客户端重连后走 snapshot 全量对账
                q.overflowed = True
        if self.webhook and not transient:
            try:
                self.webhook_queue.put_nowait(ev)
            except queue.Full:
                self._note_webhook_drop()

    def _note_webhook_drop(self):
        """队列满时丢弃新事件；计数限频打印，避免日志刷屏。"""
        self.webhook_dropped += 1
        now = util.now_ts()
        if now - self.webhook_dropped_logged > 60:
            dropped = self.webhook_dropped
            self.webhook_dropped_logged = now
            print(f"[webhook] 队列已满，累计丢弃 {dropped} 个事件", flush=True)

    def _webhook_worker(self):
        while True:
            try:
                ev = self.webhook_queue.get(timeout=1)
            except queue.Empty:
                # 关闭后不再无限等待新事件；已排队事件在下一轮取完
                if self.shutdown_event.is_set():
                    return
                continue
            self._post(ev)

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
        # 回合时长近似为最后用户输入到回合结束；供前端过滤短回合通知
        duration = max(0, util.now_ts() - sess.last_input_ts) if sess.last_input_ts else None
        self.emit(sess, "turn_finished", {"last": sess.last_assistant, "duration": duration})

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
        ts_epoch = util._parse_iso(ts)
        if ts_epoch:
            if sess.first_ts is None or ts_epoch < sess.first_ts:
                sess.first_ts = ts_epoch
            sess.last_event_ts = max(sess.last_event_ts, ts_epoch)
        elif self.first_scan_done:
            sess.last_event_ts = util.now_ts()
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
                if obj.get("isSidechain"):
                    return
                sess.saw_turn_duration = True
                sess.last_turn_end_ts = ts_epoch or util.now_ts()
                # turn_duration 是权威结束标记：清掉可能因中断而没有结果的 tool id
                sess.pending_tools.clear()
                # 中断的 Agent 调用不会再有 tool_result：运行中的子代理条目一并收尾，
                # 否则“运行中”计数和脉动圆点会永久残留；异步子代理跨回合运行，
                # 只认 task-notification，不在此清扫
                for sub in sess.subagents:
                    if sub.get("state") == "running" and not sub.get("async"):
                        sub["state"] = "done"
                        self.emit(sess, "subagent_done",
                                  {"tid": sub.get("tid"), "state": "done",
                                   "type": sub.get("type"),
                                   "description": sub.get("description")})
                # active_agents 同步收敛到仍在运行的异步条目：同步残留的 tid
                # 不清掉会让 _set_idle 永不触发、会话永久停在 working
                sess.active_agents.intersection_update(
                    sub.get("tid") for sub in sess.subagents
                    if sub.get("state") == "running" and sub.get("async"))
                sess.last_api_error = None
                if not sess.active_agents:
                    self._set_idle(sess)
            elif subtype == "compact_boundary":
                sess.compacted_at = ts_epoch or util.now_ts()
                self.emit(sess, "context_compacted", {"at": sess.compacted_at})
            elif subtype in ("api_error", "api_retry"):
                # 上游 503/限流时 CLI 只写这些 system 行，没有 assistant 内容；
                # 记录下来让网页能说明“为什么没反应”（重试会持续到 maxRetries）
                err = obj.get("error")
                if isinstance(err, dict):
                    text = err.get("message") or err.get("formatted") or ""
                else:
                    text = str(err) if err else ""
                data = {
                    "text": clip(text or "模型服务暂时不可用", 300),
                    "retry_attempt": obj.get("retryAttempt"),
                    "max_retries": obj.get("maxRetries"),
                    "retry_in_ms": obj.get("retryInMs"),
                }
                sess.last_api_error = {"ts": ts_epoch or util.now_ts(), **data}
                # 请求还在退避重试，回合没有结束
                self._set_working(sess)
                self.emit(sess, "api_error", data)
            return

        if typ == "queue-operation":
            # 新版 CLI 把 task-notification 写成队列操作行（content 为字符串）
            # 而非 user 消息；后台异步子代理的完成信号同样在这里
            content = obj.get("content")
            if isinstance(content, str) \
                    and content.lstrip().startswith("<task-notification>"):
                self._on_task_notification(sess, content)
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
                    self._on_tool_result(sess, b, obj.get("toolUseResult"))
            if kind == "system" and any(
                    (b.get("text") or "").lstrip().startswith("<task-notification>")
                    for b in blocks if isinstance(b, dict)):
                note = " ".join(b.get("text", "") for b in blocks
                                if isinstance(b, dict) and b.get("type") == "text")
                self._on_task_notification(sess, note)
            if kind == "user" and not sidechain:
                text = " ".join(b.get("text", "") for b in blocks
                                if isinstance(b, dict) and b.get("type") == "text")
                if text:
                    sess.user_msgs += 1
                    sess.last_api_error = None
                    sess.last_input_ts = ts_epoch or util.now_ts()
                    if sess.first_user is None:
                        sess.first_user = clip(text, 80)
                    sess.last_user = clip(text)
                    self._set_working(sess)
                    self.emit(sess, "user_message", {"text": sess.last_user})
            elif sidechain or kind in ("tool", "injected", "system", "meta_tool"):
                # 中断标记说明退避重试已经结束，别再显示“正在重试”
                if kind == "system" and any(
                        isinstance(b, dict) and (b.get("text") or "").lstrip().startswith("[Request interrupted")
                        for b in blocks):
                    sess.last_api_error = None
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
            if ctx > 0 and not sidechain:
                sess.context_tokens = ctx
            blocks = content if isinstance(content, list) else []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text") and not sidechain:
                    sess.last_assistant = clip(b["text"])
                    sess.last_api_error = None   # 重试成功，已有正常输出
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
                   "ts": util.now_ts(),
                   "tid": tid or "",
                   # 无 tid 无法配对结果，直接视为已完成
                   "state": "running" if tid else "done"}
            sess.subagents.append(sub)
            # 发副本：条目稍后会被 _on_tool_result 原地改 state，
            # 直接引用会让已入队/入 recent 的 start 事件也变成最终状态
            self.emit(sess, "subagent_start", dict(sub))
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

    def _on_tool_result(self, sess, b, tur=None):
        tid = b.get("tool_use_id")
        # 后台异步 Agent 的 tool_result 只是"已启动"确认（行级 toolUseResult.isAsync），
        # 真正的完成信号是后续 task-notification；期间保持 running 和 working
        async_launch = isinstance(tur, dict) and bool(
            tur.get("isAsync") or tur.get("status") == "async_launched")
        entry = next((s for s in sess.subagents
                      if s.get("tid") == tid and s.get("state") == "running"), None)
        # 同步 Agent 的结果回传才解除 working；异步启动确认不算结束
        if not (async_launch and entry is not None):
            sess.active_agents.discard(tid)
        sess.pending_tools.discard(tid)
        if b.get("is_error"):
            self.emit(sess, "tool_error", {"tool_use_id": tid,
                                           "error": clip(b.get("content", ""), 200)})
        if entry is not None:
            if async_launch:
                entry["async"] = True
            else:
                entry["state"] = "error" if b.get("is_error") else "done"
                self.emit(sess, "subagent_done",
                          {"tid": tid, "state": entry["state"],
                           "type": entry.get("type"),
                           "description": entry.get("description")})

    def _on_task_notification(self, sess, text):
        """后台异步 Agent 的完成通知：按 tool-use-id 配对收尾对应条目。"""
        m = TASK_TOOL_USE_ID_RE.search(text)
        if not m:
            return
        tid = m.group(1)
        sess.active_agents.discard(tid)
        sess.pending_tools.discard(tid)
        for sub in sess.subagents:
            if sub.get("tid") == tid and sub.get("state") == "running":
                sub["state"] = "done"
                self.emit(sess, "subagent_done",
                          {"tid": tid, "state": "done",
                           "type": sub.get("type"),
                           "description": sub.get("description")})
                break

    # ---------- 轮询文件 ----------
    def _scan_cache_obj(self):
        """延迟初始化扫描缓存，避免不扫描文件的测试读写真实缓存。"""
        if self.scan_cache is None:
            self.scan_cache = ScanCache(config.CLAUDE_DIR / "claude-sessions" / "scan-cache.json")
            self.scan_cache_entries = self.scan_cache.load()
            if self.scan_cache.error:
                print(f"[scan-cache] {self.scan_cache.error}", flush=True)
            else:
                print(f"[scan-cache] 已加载 {len(self.scan_cache_entries)} 个会话状态", flush=True)
        return self.scan_cache_entries

    @staticmethod
    def _read_tail(path, offset):
        """读取 offset 之前的最后 4 KiB，用于校验缓存前缀未被重写。"""
        if offset <= 0:
            return b""
        start = max(0, offset - config.SCAN_TAIL_SAMPLE_BYTES)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                return f.read(offset - start)
        except OSError:
            return None

    @staticmethod
    def _new_session(sid, path, parent_id, is_subagent):
        sess = Session(sid, path)
        sess.is_subagent = is_subagent
        sess.parent_id = parent_id
        return sess

    def _restore_or_new(self, path, sid, parent_id, is_subagent, size):
        """命中有效缓存时恢复累计状态；否则返回全新的 Session。"""
        entry = (self.scan_cache_entries or {}).get(str(path))
        if entry and size >= entry["offset"]:
            tail = self._read_tail(path, entry["offset"])
            if tail is not None \
                    and hashlib.sha256(tail).hexdigest() == entry.get("tail_sha256"):
                try:
                    buf = base64.b64decode(entry.get("buf", ""), validate=True)
                except (ValueError, TypeError):
                    return self._new_session(sid, path, parent_id, is_subagent)
                sess = self._new_session(sid, path, parent_id, is_subagent)
                sess.restore_cache(entry.get("session"))
                sess.offset = entry["offset"]
                sess.buf = buf
                sess.tail_sample = tail
                return sess
        return self._new_session(sid, path, parent_id, is_subagent)

    def _scan_path(self, path, sid, parent_id, is_subagent):
        """同一路径由 scan_lock 串行化整个读取-提交过程（轮询线程与
        ensure_subagent 的 HTTP 线程可能并发扫同一文件）。"""
        key = str(path)
        with self.scan_locks_guard:
            scan_lock = self.scan_locks.setdefault(key, threading.Lock())
        with scan_lock:
            self._scan_path_locked(path, sid, parent_id, is_subagent)

    def _scan_path_locked(self, path, sid, parent_id, is_subagent):
        """增量读取单个转录文件；缓存命中时只解析新增字节。

        调用方持有该路径的 scan_lock；stat、读取文件和缓存尾部校验都在
        mon.lock 外执行，只有注册、截断判定和 offset 提交持锁。
        """
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            return
        with self.lock:
            sess = self.sessions.get(key)
        if sess is None:
            entry = (self.scan_cache_entries or {}).get(key)
            if entry and size < entry["offset"]:
                # 缓存比当前文件还长（截断，常见于清空文件）：丢弃缓存并标记落盘
                sess = self._new_session(sid, path, parent_id, is_subagent)
                with self.lock:
                    self.sessions[key] = sess
                    self.emit(sess, "session_discovered", {"path": str(path)})
                    self.cache_dirty = True
            else:
                # 缓存命中校验（尾部读取）是磁盘 I/O：在锁外构造 Session
                sess = self._restore_or_new(path, sid, parent_id, is_subagent, size)
                with self.lock:
                    self.sessions[key] = sess
                    self.emit(sess, "session_discovered", {"path": str(path)})
        with self.lock:
            if size < sess.offset:
                # 文件被截断/重写：丢弃全部派生状态，避免二次累计
                sess = self.sessions[key] = self._new_session(
                    sid, path, parent_id, is_subagent)
                self.cache_dirty = True
            if size == sess.offset:
                return
            offset = sess.offset
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            # 文件在 stat 与 open 之间被删除/轮转：跳过本次，下轮重试
            return
        with self.lock:
            # scan_lock 已保证同路径串行：sess 不会被并发重置，直接提交
            data = sess.buf + chunk
            *lines, sess.buf = data.split(b"\n")   # 最后一段可能是半行，留到下次
            sess.offset = offset + len(chunk)
            if chunk:
                if len(chunk) >= config.SCAN_TAIL_SAMPLE_BYTES:
                    sess.tail_sample = chunk[-config.SCAN_TAIL_SAMPLE_BYTES:]
                else:
                    sess.tail_sample = (sess.tail_sample + chunk)[-config.SCAN_TAIL_SAMPLE_BYTES:]
            self.cache_dirty = True
            for ln in lines:
                if ln.strip():
                    self.handle_line(sess, ln.decode("utf-8", "replace"))

    def scan_once(self, include_subagents=None):
        self._scan_cache_obj()
        root = config.CLAUDE_DIR / "projects"
        if not root.exists():
            return
        if include_subagents is None:
            include_subagents = self.scan_subagents
        paths = list(root.glob("*/*.jsonl"))
        if include_subagents:
            # 子代理转录：projects/<proj>/<parent-sid>/subagents/agent-*.jsonl
            paths.extend(root.glob("*/*/subagents/agent-*.jsonl"))
        for path in paths:
            is_subagent = path.parent.name == "subagents"
            if is_subagent:
                parent_id = path.parent.parent.name
                stem = path.stem
                suffix = stem[6:] if stem.startswith("agent-") else stem
                sid = f"{parent_id}::agent-{suffix}"
            else:
                parent_id, sid = None, path.stem
            self._scan_path(path, sid, parent_id, is_subagent)
        if self._merge_task_dirs():
            self.cache_dirty = True
        self.maybe_save_cache(force=not self.first_scan_done)

    def scan_subagents_once(self, force=False):
        """显式请求子代理时一次性加载；默认轮询不会走到这里。"""
        self._scan_cache_obj()
        with self.subagent_scan_lock:
            now = util.now_ts()
            if not force and now - self.subagents_loaded_at < 5:
                return
            root = config.CLAUDE_DIR / "projects"
            if root.exists():
                for path in root.glob("*/*/subagents/agent-*.jsonl"):
                    parent_id = path.parent.parent.name
                    stem = path.stem
                    suffix = stem[6:] if stem.startswith("agent-") else stem
                    self._scan_path(path, f"{parent_id}::agent-{suffix}", parent_id, True)
            self.subagents_loaded_at = util.now_ts()
            self.maybe_save_cache(force=True)

    def ensure_subagent(self, sid):
        """按 id 懒加载单个子代理转录，供 /sessions/<parent::agent-x> 使用。"""
        if "::agent-" not in sid:
            return False
        parent_id, suffix = sid.split("::agent-", 1)
        if not parent_id or not suffix or parent_id in (".", "..") \
                or suffix in (".", "..") or "/" in parent_id or "\\" in parent_id \
                or "/" in suffix or "\\" in suffix:
            return False
        self._scan_cache_obj()
        with self.lock:
            parent = self.by_sid(parent_id)
        candidates = []
        if parent:
            candidates.append(parent.path.parent / parent_id / "subagents"
                              / f"agent-{suffix}.jsonl")
        else:
            try:
                for proj in (config.CLAUDE_DIR / "projects").iterdir():
                    fp = proj / parent_id / "subagents" / f"agent-{suffix}.jsonl"
                    if fp.is_file():
                        candidates.append(fp)
            except OSError:
                pass
        for path in candidates:
            if path.is_file():
                self._scan_path(path, sid, parent_id, True)
        self.maybe_save_cache()
        with self.lock:
            return self.by_sid(sid) is not None

    def subagent_ids(self, sess):
        """列出父会话的子代理 id；只读目录，不需要解析子代理内容。"""
        ids = set()
        sub_dir = sess.path.parent / sess.id / "subagents"
        try:
            for fp in sub_dir.glob("agent-*.jsonl"):
                stem = fp.stem
                suffix = stem[6:] if stem.startswith("agent-") else stem
                ids.add(f"{sess.id}::agent-{suffix}")
        except OSError:
            pass
        with self.lock:
            ids.update(s.id for s in self.sessions.values() if s.parent_id == sess.id)
        return sorted(ids)

    def maybe_save_cache(self, force=False):
        if not self.cache_dirty or self.scan_cache is None:
            return
        interval = (config.SCAN_CACHE_SAVE_SUBAGENTS_SECONDS if self.scan_subagents
                    else config.SCAN_CACHE_SAVE_SECONDS)
        if not force and util.now_ts() - self.last_cache_save < interval:
            return
        self.save_scan_cache()

    def save_scan_cache(self):
        if self.scan_cache is None:
            return
        with self.lock:
            entries = {}
            for key, sess in self.sessions.items():
                # 默认只缓存主会话，避免开启全量扫描时反复写数 MB 子代理状态
                if sess.is_subagent and not self.scan_subagents:
                    continue
                entries[key] = {
                    "offset": sess.offset,
                    "tail_sha256": hashlib.sha256(sess.tail_sample).hexdigest(),
                    "buf": base64.b64encode(sess.buf).decode("ascii"),
                    "session": sess.cache_state(),
                }
            self.cache_dirty = False
            self.last_cache_save = util.now_ts()
        try:
            self.scan_cache.save(entries)
            self.scan_cache_entries = self.scan_cache.files
        except (OSError, TypeError, ValueError) as e:
            self.cache_dirty = True
            print(f"[scan-cache] 保存失败: {e}", flush=True)

    def _merge_task_dirs(self):
        """~/.claude/tasks/<sid>/*.json 若存在，以它为准校正任务列表。

        任务文件读取在 mon.lock 外执行；提交前确认 Session 未被扫描线程
        截断重置，被替换的条目本轮跳过。
        """
        troot = config.CLAUDE_DIR / "tasks"
        if not troot.exists():
            return False
        with self.lock:
            snapshot = [(key, sess, troot / sess.id)
                        for key, sess in self.sessions.items()
                        if not sess.is_subagent]
        changed = False
        for key, sess, d in snapshot:
            if not d.is_dir():
                continue
            tasks = {}
            for fp in sorted(d.glob("*.json")):
                try:
                    t = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(t, dict) and "id" in t:
                    tasks[str(t["id"])] = {"id": str(t["id"]), "subject": t.get("subject", ""),
                                           "description": t.get("description", ""),
                                           "status": t.get("status", "pending")}
            with self.lock:
                if self.sessions.get(key) is not sess:
                    continue
                # 目录存在但文件被清空时也要清掉旧任务；目录不存在时保留转录推断结果
                if tasks != sess.tasks:
                    sess.tasks = tasks
                    changed = True
        return changed

    def run(self, interval):
        while not self.shutdown_event.is_set():
            try:
                self.scan_once()
            except Exception as e:
                print(f"[scan] 错误: {e}")
            if not self.first_scan_done:
                self.first_scan_done = True
                print(f"[monitor] 初次扫描完成，发现 {len(self.sessions)} 个会话")
            # 推迟睡眠每 15 秒评估一次；开关切换时 set_keep_awake 会立即评估
            if util.now_ts() - self.last_awake_check >= 15:
                self.last_awake_check = util.now_ts()
                self._apply_keep_awake()
            # 孤儿进程每 60 秒采样一次后代树（退出时 ppid 可能已断链）
            if util.now_ts() - self.last_orphan_sample >= 60:
                self.last_orphan_sample = util.now_ts()
                self.orphans.sample(self.hosted)
            self.shutdown_event.wait(interval)
        self.maybe_save_cache(force=True)
        # 服务退出：解除推迟睡眠（进程退出本身也会失效，这里尽早恢复正常）
        if self.awake_held:
            try:
                system.set_keep_awake(False)
                self.awake_held = False
            except (OSError, system.SystemInfoUnavailable):
                pass
