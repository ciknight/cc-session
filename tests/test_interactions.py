import queue
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


class InteractionTests(unittest.TestCase):
    def setUp(self):
        try:
            # Windows 上后台线程可能仍在关闭文件，清理失败不应让测试失败
            self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:   # Python 3.9
            self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(monitor, "CLAUDE_DIR", self.root / "config")
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
        with patch.object(monitor.subprocess, "Popen", side_effect=popen):
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