import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import monitor
from claude_sessions import config, hosted


class InteractionTests(unittest.TestCase):
    def setUp(self):
        try:
            # Windows 上后台线程可能仍在关闭文件，清理失败不应让测试失败
            self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:   # Python 3.9
            self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(config, "CLAUDE_DIR", self.root / "config")
        self.patch.start()
        self.mon = monitor.Monitor(600, permission_timeout=0.3)
        self.sess = monitor.Session("fake-session", self.root / "fake.jsonl")
        self.sess.cwd = str(self.root)
        self.mon.sessions[str(self.sess.path)] = self.sess
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.stop(kill=True)
            if host.proc:
                try:
                    host.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.patch.stop()
        self.tmp.cleanup()

    def start_host(self, text="first", approval="ask"):
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **kwargs):
            return original([sys.executable, fake] + cmd[1:], **kwargs)
        with patch.object(hosted.subprocess, "Popen", side_effect=popen):
            code, resp = self.mon.host_session(str(self.root), None, text,
                                               model="opus", approval=approval)
        self.assertEqual(code, 202)
        host = self.mon.hosted[resp["hosted_id"]]
        self.hosts.append(host)
        return host, resp["job_id"]

    def wait_for(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_permission_allow_once(self):
        host, job = self.start_host("permission please")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        pending = self.mon.pending_interactions("fake-session")[0]
        self.assertEqual(pending["tool_name"], "Edit")
        self.assertIn("-old", pending["preview"]["text"])
        code, _ = self.mon.respond_interaction(pending["id"], {"behavior": "allow"})
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertEqual(self.mon.jobs[job]["result"], "decision:allow")

    def test_permission_timeout_denies(self):
        host, job = self.start_host("permission please")
        self.assertTrue(self.wait_for(
            lambda: self.mon.jobs[job]["status"] in ("done", "failed", "cancelled")))
        self.assertEqual(self.mon.jobs[job]["result"], "decision:deny")

    def test_full_mode_auto_allows(self):
        host, job = self.start_host("permission please", approval="full")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertEqual(self.mon.jobs[job]["result"], "decision:allow")

    def test_write_to_closed_stdin_converts_value_error(self):
        # stop 关闭 stdin 后并发写入会抛 ValueError("write to closed file")，
        # 不转换就会逃过 _send_loop 的 (BrokenPipeError, OSError) 处理、线程裸死
        host = monitor.Hosted(self.mon, str(self.root), None)
        stream = io.BytesIO()
        stream.close()
        host.proc = SimpleNamespace(stdin=stream)
        with self.assertRaises(BrokenPipeError):
            host._write({"type": "user"})

    def test_stop_closes_stdin_under_write_lock(self):
        host = monitor.Hosted(self.mon, str(self.root), None)
        calls = []
        host.proc = SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: calls.append("close")),
            wait=lambda timeout=None: calls.append("wait"),
            terminate=lambda: calls.append("terminate"),
            poll=lambda: None)
        host.status = "running"
        held = threading.Event()
        release = threading.Event()

        def hold_write_lock():
            with host.write_lock:
                held.set()
                release.wait(2)

        holder = threading.Thread(target=hold_write_lock)
        holder.start()
        self.assertTrue(held.wait(1))
        stopped = threading.Event()

        def do_stop():
            host.stop()
            stopped.set()

        threading.Thread(target=do_stop, daemon=True).start()
        time.sleep(0.1)
        self.assertEqual(calls, [], "close stdin 必须等待 write_lock 释放")
        release.set()
        holder.join(2)
        self.assertTrue(stopped.wait(2))
        self.assertEqual(calls, ["close", "wait"])

    def test_concurrent_resolve_allows_exactly_one_winner(self):
        # 用户响应与超时/中断并发到达时，resolve 的检查与置位必须原子：
        # 只有一方首次成功，另一方拿到 False
        hosted = type("H", (), {"hid": "h1", "session_id": "s"})()
        inter = monitor.Interaction("r1", hosted, {"tool_name": "Bash"})
        barrier = threading.Barrier(2)

        def attempt(behavior):
            barrier.wait()
            return inter.resolve(behavior)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, "allow"), pool.submit(attempt, "deny")]
            results = [future.result() for future in futures]

        self.assertEqual(sum(1 for r in results if r), 1)
        winners = [behavior for behavior, r in zip(("allow", "deny"), results) if r]
        self.assertEqual(inter.state, "allowed" if winners[0] == "allow" else "denied")

    def test_allow_always_requires_suggestion(self):
        host, job = self.start_host("permission please")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        pending = self.mon.pending_interactions("fake-session")[0]
        self.assertTrue(pending["allow_always"])

    def test_queue_limit(self):
        host, first = self.start_host("interrupt-me")
        host.max_queued = 2
        self.assertTrue(self.wait_for(lambda: host.current is not None))
        self.assertEqual(host.send("a")[0], 202)
        self.assertEqual(host.send("b")[0], 202)
        code, resp = host.send("c")
        self.assertEqual(code, 429)
        self.assertIn("queue", resp["error"].lower())

    def test_hosted_registers_ask_user_hook(self):
        self.mon.http_port = 12345
        self.mon.hook_token = "tok"
        captured = {}
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))

        def popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return original([sys.executable, fake] + cmd[1:], **kwargs)

        with patch.object(hosted.subprocess, "Popen", side_effect=popen):
            code, resp = self.mon.host_session(str(self.root), None, "first",
                                               model="opus", approval="ask")
        self.assertEqual(code, 202)
        host = self.mon.hosted[resp["hosted_id"]]
        self.hosts.append(host)
        cmd = captured["cmd"]
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        rule = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(rule["matcher"], "AskUserQuestion")
        self.assertIn(" hook ", rule["hooks"][0]["command"])
        self.assertIn("--token", rule["hooks"][0]["command"])

    def test_hosted_merges_existing_inline_settings(self):
        self.mon.http_port = 12345
        self.mon.hook_token = "tok"
        self.mon.claude_args = ["--settings", json.dumps(
            {"permissions": {"allow": ["Bash"]}})]
        captured = {}
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))

        def popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return original([sys.executable, fake] + cmd[1:], **kwargs)

        with patch.object(hosted.subprocess, "Popen", side_effect=popen):
            code, resp = self.mon.host_session(str(self.root), None, "first",
                                               model="opus", approval="ask")
        self.assertEqual(code, 202)
        self.hosts.append(self.mon.hosted[resp["hosted_id"]])
        cmd = captured["cmd"]
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertEqual(settings["permissions"], {"allow": ["Bash"]})
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"],
                         "AskUserQuestion")

    def test_hook_command_posix_paths_for_bash(self):
        # Claude Code 在 Windows 上经 Git Bash 执行 hook，反斜杠路径会被 bash
        # 当转义吃掉（生产事故：F:\code\... → F:code...，exit 127，hook 永远失败）
        self.mon.http_port = 12345
        self.mon.hook_token = "tok"
        cmd = self.mon.hook_command()
        self.assertNotIn("\\", cmd)
        self.assertIn(" hook ", f" {cmd} ")
        self.assertIn("--token", cmd)

    def test_can_use_tool_ask_user_question_is_choice(self):
        # PreToolUse hook 失败时 AskUserQuestion 降级走 can_use_tool；
        # 仍应构造为可作答的 choice 卡，而不是只能整卡放行的 permission 卡
        host, job = self.start_host("ask")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        pending = self.mon.pending_interactions("fake-session")[0]
        self.assertEqual(pending["kind"], "choice")
        self.assertEqual([q["question"] for q in pending["questions"]], ["Q1"])
        code, _ = self.mon.respond_interaction(
            pending["id"], {"behavior": "allow", "answers": {"Q1": "A"}})
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertEqual(self.mon.jobs[job]["result"], "decision:allow")

    def test_exit_plan_mode_is_plan_kind(self):
        host, job = self.start_host("plan")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        pending = self.mon.pending_interactions("fake-session")[0]
        self.assertEqual(pending["kind"], "plan")
        self.assertFalse(pending["allow_always"])
        self.assertIn("Plan", pending["preview"]["text"])
        code, _ = self.mon.respond_interaction(
            pending["id"], {"behavior": "deny", "reason": "keep planning"})
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(
            lambda: self.mon.jobs[job]["status"] in ("done", "failed", "cancelled")))

    def test_stream_text_is_aggregated_and_cleared(self):
        self.mon.first_scan_done = True
        q = queue.Queue()
        self.mon.subscribers.append(q)
        host, job = self.start_host("stream")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertEqual(host.info()["stream_text"], "")
        self.assertFalse(host.info()["streaming"])
        deltas = []
        end = time.time() + 2
        while time.time() < end:
            try:
                ev = q.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
                continue
            if ev["type"] == "turn_delta":
                deltas.append(ev["data"]["text"])
                if "世界" in ev["data"]["text"]:
                    break
        self.assertTrue(deltas)
        self.assertIn("世界", deltas[-1])

    def test_api_retry_is_recorded_for_diagnostics(self):
        host, job = self.start_host("api-retry")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        retry = host.info().get("last_api_retry")
        self.assertEqual(retry["error_status"], 503)
        self.assertEqual(retry["attempt"], 1)

    def test_lifecycle_completed_before_result(self):
        host, job = self.start_host("lifecycle-first")
        self.assertTrue(self.wait_for(
            lambda: self.mon.jobs[job]["status"] == "done", timeout=2))
        self.assertEqual(self.mon.jobs[job]["result"], "lifecycle-first")

    def test_set_approval_switches_live(self):
        host, job = self.start_host("first")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        host.set_approval("auto")
        self.assertEqual(host.approval, "auto")
        host.set_approval("ask")
        self.assertEqual(host.approval, "ask")

    def test_auto_switch_from_ask_sets_and_clears_flag(self):
        host, job = self.start_host("first", approval="ask")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        calls = []
        host._control = lambda subtype, **params: calls.append((subtype, params))
        host.set_approval("auto")
        self.assertTrue(host.added_auto_permission)
        host.set_approval("ask")
        self.assertFalse(host.added_auto_permission)
        self.assertEqual(calls, [("set_permission_mode", {"mode": "auto"}),
                                 ("set_permission_mode", {"mode": "default"})])

    def test_ask_switch_keeps_user_permission_mode(self):
        self.mon.claude_args = ["--permission-mode", "acceptEdits"]
        host, job = self.start_host("first", approval="auto")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertFalse(host.added_auto_permission)
        calls = []
        host._control = lambda subtype, **params: calls.append((subtype, params))
        host.set_approval("ask")
        self.assertEqual(calls, [])
        self.assertEqual(host.approval, "ask")

    def test_stop_mid_turn_does_not_leave_job_running(self):
        host, job = self.start_host("slow")
        self.assertTrue(self.wait_for(lambda: host.current is not None))
        host.stop()
        self.assertTrue(self.wait_for(
            lambda: self.mon.jobs[job]["status"] in ("done", "failed", "cancelled")))

    def test_stop_cancels_pending_interactions(self):
        host, job = self.start_host("permission please")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        host.stop(kill=True)
        self.assertTrue(self.wait_for(
            lambda: not self.mon.pending_interactions("fake-session")))
        self.assertTrue(self.wait_for(
            lambda: self.mon.jobs[job]["status"] in ("failed", "cancelled")))

    def test_interrupt_clears_queue_and_cancels_current(self):
        host, first = self.start_host("interrupt-me")
        _, queued = host.send("queued")
        self.assertTrue(self.wait_for(lambda: "fake-session" in self.mon.hosted_by_sid))
        code, _ = self.mon.interrupt_session("fake-session")
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[first]["status"] == "cancelled"))
        self.assertEqual(self.mon.jobs[queued["job_id"]]["status"], "cancelled")
        self.assertTrue(host.alive())


if __name__ == "__main__":
    unittest.main()