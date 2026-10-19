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


if __name__ == "__main__":
    unittest.main()