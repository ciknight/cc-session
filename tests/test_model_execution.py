import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
from claude_sessions import config, core, hosted


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        try:
            # Windows 上后台线程可能仍在关闭文件，清理失败不应让测试失败
            self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:   # Python 3.9
            self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_patch = patch.object(config, "CLAUDE_DIR", self.root / "config")
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
        with patch.object(hosted.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), None, text, model=model)
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        return host, response["job_id"]

    def test_oneshot_model_overrides_service_args(self):
        self.mon.claude_args = ["--model=haiku", "--verbose"]
        completed = subprocess.CompletedProcess([], 0, json.dumps({"result": "ok"}), "")
        with patch.object(core.subprocess, "run", return_value=completed) as run:
            code, response = self.mon.send_message("existing", "hello", model="vendor-model-7")
            self.assertEqual(code, 202)
            job = self.wait_done(response["job_id"])
        self.assertEqual(job["requested_model"], "vendor-model-7")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-1], "--model=vendor-model-7")
        self.assertNotIn("--model=haiku", cmd)
        self.assertEqual(self.mon.preferences.get("existing"), "vendor-model-7")

    def test_oneshot_messages_queue_while_running(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fake_exec(self, job, cmd, cwd, text):
            calls.append(text)
            if text == "first":
                started.set()
                release.wait(5)
            job["result"] = text
            job["status"] = "done"
            job["finished_at"] = monitor.now_ts()

        with patch.object(monitor.Monitor, "_exec", fake_exec):
            code, first = self.mon.send_message("existing", "first")
            self.assertEqual(code, 202)
            self.assertTrue(started.wait(2))
            code, second = self.mon.send_message("existing", "second")
            self.assertEqual(code, 202)
            self.assertEqual(second["status"], "queued")
            release.set()
            end = time.time() + 5
            while time.time() < end:
                if (self.mon.jobs[first["job_id"]]["status"] == "done"
                        and self.mon.jobs[second["job_id"]]["status"] == "done"):
                    break
                time.sleep(0.01)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(self.mon.jobs[first["job_id"]]["status"], "done")
        self.assertEqual(self.mon.jobs[second["job_id"]]["status"], "done")
        with self.mon.lock:
            self.assertNotIn("existing", self.mon.busy)
            self.assertNotIn("existing", self.mon.remote_queues)

    def test_working_session_requires_force_for_oneshot(self):
        self.sess.state = "working"
        code, _ = self.mon.send_message("existing", "hello")
        self.assertEqual(code, 409)
        completed = subprocess.CompletedProcess([], 0, json.dumps({"result": "ok"}), "")
        with patch.object(core.subprocess, "run", return_value=completed):
            code, response = self.mon.send_message("existing", "hello", force=True)
            self.assertEqual(code, 202)
            job = self.wait_done(response["job_id"])
        self.assertEqual(job["status"], "done")

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

    def test_queue_cancel_is_atomic_and_preserves_fifo_models(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def run(cmd, **kwargs):
            calls.append((kwargs['input'], cmd[-1]))
            if kwargs['input'] == 'first':
                entered.set()
                self.assertTrue(release.wait(5))
            if kwargs['input'] == 'third':
                finished.set()
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': 'ok'}), '')
        with patch.object(core.subprocess, 'run', side_effect=run):
            _, first = self.mon.send_message('existing', 'first', model='opus')
            self.assertTrue(entered.wait(2))
            _, middle = self.mon.send_message('existing', 'middle', model='sonnet')
            _, third = self.mon.send_message('existing', 'third', model='haiku')
            code, snapshot = self.mon.session_jobs('existing')
            self.assertEqual(code, 200)
            self.assertEqual([j['position'] for j in snapshot['queued']], [1, 2])
            self.assertEqual(self.mon.cancel_job('other', middle['job_id'])[0], 404)
            self.assertEqual(self.mon.cancel_job('existing', first['job_id'])[0], 409)
            self.assertEqual(self.mon.cancel_job('existing', middle['job_id'])[0], 200)
            self.assertEqual(self.mon.cancel_job('existing', middle['job_id'])[0], 200)
            self.assertEqual(self.mon.session_jobs('existing')[1]['queued'][0]['position'], 1)
            release.set()
            self.assertTrue(finished.wait(2))
            self.wait_done(third['job_id'])
        self.assertEqual(calls, [('first', '--model=opus'), ('third', '--model=haiku')])

    def test_registered_job_is_visible_but_not_cancellable_and_history_is_bounded(self):
        active = self.mon._new_job('existing', 'unpublished')
        self.assertEqual(self.mon.cancel_job('existing', active['id'])[0], 409)
        item = self.mon.session_jobs('existing')[1]['queued'][0]
        self.assertFalse(item['cancellable'])
        self.assertIsNone(item['position'])
        for i in range(105):
            job = self.mon._new_job('existing', str(i))
            with self.mon.lock:
                self.mon._terminal_job_locked(job, 'done')
                self.mon._terminal_job_locked(job, 'failed')
        self.assertIn(active['id'], self.mon.jobs)
        self.assertEqual(len(self.mon.jobs), 101)
        self.assertEqual(len(self.mon.session_jobs('existing')[1]['recent']), 20)
        copy = self.mon.job_snapshot(active['id'])
        copy['status'] = 'corrupted'
        self.assertEqual(active['status'], 'queued')
        self.assertNotIn('_owner', copy)

    def test_host_cancel_does_not_finish_current_turn_or_clear_stream(self):
        host = monitor.Hosted(self.mon, str(self.root), 'existing')
        host.status = 'running'
        _, first = host.send('first', 'opus')
        _, middle = host.send('middle', 'sonnet')
        _, third = host.send('third', 'haiku')
        with host.ready:
            _, current, _ = host.pending.popleft()
            host.current = current
            current['status'] = 'running'
            host.stream_job, host.stream_text = current, 'partial'
        with patch.object(host, '_write') as write:
            self.assertEqual(self.mon.cancel_job('existing', middle['job_id'])[0], 200)
            write.assert_not_called()
        self.assertIs(host.current, current)
        self.assertEqual(host.stream_text, 'partial')
        self.assertEqual(host.turns, 0)
        self.assertEqual([j['id'] for j in self.mon.session_jobs('existing')[1]['queued']], [third['job_id']])
        host._on_init('corrected')
        self.assertEqual(self.mon.session_jobs('corrected')[0], 200)
        self.assertEqual(self.mon.jobs[middle['job_id']]['session'], 'corrected')

    def test_host_claim_wins_during_model_switch_and_cancel_wins_before_claim(self):
        host = monitor.Hosted(self.mon, str(self.root), 'existing', selected_model='opus')
        host.status = 'running'
        entered, release, written = threading.Event(), threading.Event(), threading.Event()
        def control(subtype, **kwargs):
            if subtype == 'set_model':
                entered.set()
                self.assertTrue(release.wait(5))
        def write(obj):
            written.set()
            with host.ready:
                host.stopping = True
                host.ready.notify_all()
        _, cancelled = host.send('cancel before claim', 'haiku')
        self.assertEqual(self.mon.cancel_job('existing', cancelled['job_id'])[0], 200)
        _, claimed = host.send('claimed', 'sonnet')
        with patch.object(host, '_control', side_effect=control), patch.object(host, '_write', side_effect=write):
            sender = threading.Thread(target=host._send_loop)
            sender.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.mon.cancel_job('existing', claimed['job_id'])[0], 409)
            snapshot = self.mon.session_jobs('existing')[1]
            self.assertEqual(snapshot['running'][0]['id'], claimed['job_id'])
            release.set()
            self.assertTrue(written.wait(2))
            sender.join(2)
            self.assertFalse(sender.is_alive())
        self.assertEqual(self.mon.jobs[cancelled['job_id']]['status'], 'cancelled')
        host._finish_job(self.mon.jobs[claimed['job_id']], 'completed')

    def test_oneshot_claim_wins_and_cancel_releases_queue_capacity(self):
        first_entered, release_first, second_entered, release_second = (threading.Event() for _ in range(4))
        def run(cmd, **kwargs):
            if kwargs['input'] == 'first':
                first_entered.set()
                self.assertTrue(release_first.wait(5))
            elif kwargs['input'] == 'second':
                second_entered.set()
                self.assertTrue(release_second.wait(5))
            return subprocess.CompletedProcess(cmd, 0, '{"result":"ok"}', '')
        with patch.object(core.subprocess, 'run', side_effect=run), patch.object(config, 'REMOTE_QUEUE_LIMIT', 1):
            self.mon.send_message('existing', 'first')
            self.assertTrue(first_entered.wait(2))
            _, cancelled = self.mon.send_message('existing', 'cancel')
            self.assertEqual(self.mon.send_message('existing', 'overflow')[0], 429)
            self.assertEqual(self.mon.cancel_job('existing', cancelled['job_id'])[0], 200)
            _, second = self.mon.send_message('existing', 'second')
            release_first.set()
            self.assertTrue(second_entered.wait(2))
            self.assertEqual(self.mon.cancel_job('existing', second['job_id'])[0], 409)
            release_second.set()
            self.wait_done(second['job_id'])

    def test_host_stop_keeps_cancelled_and_old_host_owner_history(self):
        from unittest.mock import Mock
        old = monitor.Hosted(self.mon, str(self.root), 'existing')
        old.status = 'running'
        old.proc = Mock()
        _, cancelled = old.send('cancel', 'opus')
        self.assertEqual(self.mon.cancel_job('existing', cancelled['job_id'])[0], 200)
        _, queued = old.send('stop', 'haiku')
        old.stop(kill=True)
        self.assertEqual(self.mon.jobs[cancelled['job_id']]['status'], 'cancelled')
        self.assertEqual(self.mon.jobs[queued['job_id']]['status'], 'failed')
        replacement = monitor.Hosted(self.mon, str(self.root), 'existing')
        replacement.status = 'running'
        self.mon.hosted_by_sid['existing'] = replacement
        _, response = replacement.send('new owner', 'sonnet')
        self.assertEqual(self.mon.cancel_job('existing', response['job_id'])[0], 200)
        history = self.mon.session_jobs('existing')[1]['recent']
        self.assertEqual({j['id'] for j in history}, {cancelled['job_id'], queued['job_id'], response['job_id']})
        self.assertEqual(len(self.mon.terminal_jobs), 3)

    def test_new_oneshot_result_binds_session_atomically(self):
        completed = subprocess.CompletedProcess([], 0, '{"session_id":"new-id","result":"ok"}', '')
        with patch.object(core.subprocess, 'run', return_value=completed):
            _, response = self.mon.new_session(str(self.root), 'new')
            self.wait_done(response['job_id'])
        snapshot = self.mon.session_jobs('new-id')[1]
        self.assertEqual(snapshot['recent'][0]['id'], response['job_id'])
        self.assertEqual(self.mon.job_snapshot(response['job_id'])['session'], 'new-id')

    def test_invalid_model_does_not_create_job(self):
        code, _ = self.mon.send_message("existing", "hi", model=[])
        self.assertEqual(code, 400)
        self.assertEqual(self.mon.jobs, {})

    def start_host_capturing(self, **kwargs):
        captured = []
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **popen_kwargs):
            captured.append(cmd)
            return original([sys.executable, fake] + cmd[1:], **popen_kwargs)
        with patch.object(hosted.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), kwargs.pop("resume_id", None),
                                                   "hi", **kwargs)
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        return host, response["job_id"], captured

    def test_hosted_effort_spawn_argument(self):
        host, job_id, captured = self.start_host_capturing(resume_id="existing", effort="high")
        self.assertIn("--effort=high", captured[0])
        self.assertEqual(self.mon.efforts.get("existing"), "high")
        self.assertEqual(host.effort, "high")
        self.wait_done(job_id)

    def test_hosted_effort_default_uses_service_args(self):
        _, job_id, captured = self.start_host_capturing(resume_id="existing")
        self.assertFalse(any(a.startswith("--effort") for a in captured[0]))
        self.wait_done(job_id)

    def test_hosted_effort_invalid_rejected(self):
        code, response = self.mon.host_session(str(self.root), "existing", "hi",
                                               effort="ultra")
        self.assertEqual(code, 400)
        self.assertIn("effort", response["error"])
        self.assertIsNone(self.mon.efforts.get("existing"))

    def test_hosted_effort_binds_new_session(self):
        _, job_id, _ = self.start_host_capturing(effort="low")
        self.wait_done(job_id)
        # fake_claude 的 init 携带 fake-session；首轮选择应绑定到该 sid
        self.assertEqual(self.mon.efforts.get("fake-session"), "low")

    def test_set_effort_switches_live_host_and_persists(self):
        host, _, _ = self.start_host_capturing(resume_id="existing")
        code, resp = self.mon.set_effort("existing", "high")
        self.assertEqual(code, 200)
        self.assertEqual(resp, {"effort": "high"})
        self.assertEqual(host.process_effort, "high")
        self.assertEqual(self.mon.efforts.get("existing"), "high")
        # default 清除偏好并回落模型默认
        code, resp = self.mon.set_effort("existing", "default")
        self.assertEqual(code, 200)
        self.assertEqual(resp, {"effort": "default"})
        self.assertIsNone(host.process_effort)
        self.assertIsNone(self.mon.efforts.get("existing"))

    def test_set_effort_invalid_value(self):
        code, resp = self.mon.set_effort("existing", "ultra")
        self.assertEqual(code, 400)
        self.assertIn("effort", resp["error"])

    def test_set_effort_without_host_only_persists(self):
        code, resp = self.mon.set_effort("existing", "medium")
        self.assertEqual(code, 200)
        self.assertEqual(self.mon.efforts.get("existing"), "medium")

    def test_set_effort_control_failure_keeps_preference(self):
        host, _, _ = self.start_host_capturing(resume_id="existing")
        with patch.object(host, "_control",
                          side_effect=RuntimeError("控制请求失败: apply_flag_settings")):
            code, resp = self.mon.set_effort("existing", "low")
        self.assertEqual(code, 200)
        self.assertTrue(resp["warning"])
        self.assertEqual(self.mon.efforts.get("existing"), "low")

    def test_hosted_commands_from_init(self):
        host, job_id = self.start_host()
        self.wait_done(job_id)
        deadline = time.time() + 2
        while not host.commands and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(host.commands, ["compact", "model", "review"])


if __name__ == "__main__":
    unittest.main()
