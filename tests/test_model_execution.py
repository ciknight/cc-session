import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_patch = patch.object(monitor, "CLAUDE_DIR", self.root / "config")
        self.config_patch.start()
        self.mon = monitor.Monitor(600)
        self.sess = monitor.Session("existing", self.root / "existing.jsonl")
        self.sess.cwd = str(self.root)
        self.sess.state = "idle"
        self.mon.sessions[str(self.sess.path)] = self.sess
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.stop(kill=True)
            if host.proc:
                host.proc.wait(timeout=5)
        self.config_patch.stop()
        self.tmp.cleanup()

    def wait_done(self, job_id):
        for _ in range(300):
            job = self.mon.jobs[job_id]
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.01)
        self.fail("任务未在测试期限内结束")

    def start_host(self, model="opus", text="first"):
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **kwargs):
            return original([sys.executable, fake] + cmd[1:], **kwargs)
        with patch.object(monitor.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), None, text, model=model)
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        return host, response["job_id"]

    def test_oneshot_model_overrides_service_args(self):
        self.mon.claude_args = ["--model=haiku", "--verbose"]
        completed = subprocess.CompletedProcess([], 0, json.dumps({"result": "ok"}), "")
        with patch.object(monitor.subprocess, "run", return_value=completed) as run:
            code, response = self.mon.send_message("existing", "hello", model="vendor-model-7")
            self.assertEqual(code, 202)
            job = self.wait_done(response["job_id"])
        self.assertEqual(job["requested_model"], "vendor-model-7")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-1], "--model=vendor-model-7")
        self.assertNotIn("--model=haiku", cmd)
        self.assertEqual(self.mon.preferences.get("existing"), "vendor-model-7")

    def test_hosted_queue_keeps_model_snapshot(self):
        host, first = self.start_host(text="slow")
        _, second = host.send("second", "sonnet")
        _, third = host.send("third", "haiku")
        self.assertEqual(self.wait_done(first)["result"], "opus:slow")
        self.assertEqual(self.wait_done(second["job_id"])["result"], "sonnet:second")
        self.assertEqual(self.wait_done(third["job_id"])["result"], "haiku:third")
        self.assertEqual(host.turns, 3)
        self.assertEqual(self.mon.preferences.get("fake-session"), "opus")

    def test_control_error_does_not_send_text_to_previous_model(self):
        host, first = self.start_host()
        self.wait_done(first)
        _, rejected = host.send("must-not-send", "reject")
        self.assertEqual(self.wait_done(rejected["job_id"])["status"], "failed")
        code, rejected = host.send("after", "sonnet")
        self.assertEqual(code, 409)
        self.assertIn("rehost", rejected["error"])
        self.assertEqual(host.turns, 1)

    def test_control_timeout_fails_queue(self):
        host, first = self.start_host()
        self.wait_done(first)
        host.control_timeout = 0.1
        _, stalled = host.send("must-not-send", "timeout")
        _, pending = host.send("queued", "haiku")
        self.assertEqual(self.wait_done(stalled["job_id"])["status"], "failed")
        self.assertEqual(self.wait_done(pending["job_id"])["status"], "failed")
        self.assertEqual(host.turns, 1)
        self.assertFalse(host.alive())

    def test_clear_hosted_override_restores_service_model(self):
        self.mon.claude_args = ["--model=haiku"]
        host, first = self.start_host()
        self.wait_done(first)
        _, response = host.send("default", None)
        self.assertEqual(self.wait_done(response["job_id"])["result"], "haiku:default")

    def test_invalid_model_does_not_create_job(self):
        code, _ = self.mon.send_message("existing", "hi", model=[])
        self.assertEqual(code, 400)
        self.assertEqual(self.mon.jobs, {})


if __name__ == "__main__":
    unittest.main()
