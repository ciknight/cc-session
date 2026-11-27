import json
import os
import queue
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
        self.assertIn("--resume=existing", cmd)
        self.assertEqual(cmd[-1], "--model=vendor-model-7")
        self.assertNotIn("--model=haiku", cmd)
        self.assertEqual(self.mon.preferences.get("existing"), "vendor-model-7")

    def test_oneshot_child_env_strips_parent_session(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps({"result": "ok"}), "")
        with patch.dict(os.environ, {"CLAUDECODE": "1",
                                     "CLAUDE_CODE_SESSION_ID": "parent",
                                     "ANTHROPIC_API_KEY": "k"}), \
                patch.object(core.subprocess, "run", return_value=completed) as run:
            code, response = self.mon.send_message("existing", "hello")
            self.assertEqual(code, 202)
            self.wait_done(response["job_id"])
        env = run.call_args.kwargs["env"]
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")
        self.assertEqual(env["PWD"], str(self.root))

    def test_oneshot_error_uses_structured_result_fields(self):
        payload = {"is_error": True, "subtype": "success",
                   "result": "API Error: 529 overloaded",
                   "errors": ["overloaded"], "api_error_status": 529,
                   "terminal_reason": "api_error"}
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch.object(core.subprocess, "run", return_value=completed):
            code, response = self.mon.send_message("existing", "hello")
            self.assertEqual(code, 202)
            job = self.wait_done(response["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("HTTP 529", job["error"])
        self.assertIn("overloaded", job["error"])

    def test_hosted_child_env_strips_parent_session(self):
        captured = {}
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))

        def popen(cmd, **kwargs):
            captured.update(kwargs)
            return original([sys.executable, fake] + cmd[1:], **kwargs)

        with patch.dict(os.environ, {"CLAUDECODE": "1",
                                     "CLAUDE_CODE_SESSION_ID": "parent",
                                     "ANTHROPIC_API_KEY": "k"}), \
                patch.object(hosted.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), None, "hi")
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        env = captured["env"]
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")
        self.assertEqual(env["PWD"], str(self.root))
        self.wait_done(response["job_id"])

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

    def _host_with_fake_stdin(self, host):
        """启动发送线程但不接真实进程；返回 (sender, written, stop)。"""
        written = []
        def write(obj):
            written.append(obj)

        patchers = [patch.object(host, "_control", return_value={}),
                    patch.object(host, "_write", side_effect=write)]
        for p in patchers:
            p.start()
        sender = threading.Thread(target=host._send_loop, daemon=True)
        sender.start()

        def stop():
            with host.ready:
                host.stopping = True
                host.ready.notify_all()
            sender.join(2)
            for p in patchers:
                p.stop()
        return sender, written, stop

    def test_host_delivers_followup_without_waiting_for_turn_end(self):
        host = monitor.Hosted(self.mon, str(self.root), 'existing')
        host.status = 'running'
        events = []
        host._emit_session_event = lambda kind, data, transient=False: events.append((kind, data))
        sender, written, stop = self._host_with_fake_stdin(host)
        try:
            _, first = host.send('first', None)
            _, second = host.send('second', None)
            deadline = time.time() + 2
            while (sum(1 for w in written if w.get('type') == 'user') < 2
                   and time.time() < deadline):
                time.sleep(0.01)
            self.assertEqual(
                sum(1 for w in written if w.get('type') == 'user'), 2,
                '两条消息都应立即写入 stdin，而不是等上一条结束')
            self.assertEqual(self.mon.jobs[second['job_id']]['status'], 'queued')
            cu1 = self.mon.jobs[first['job_id']]['command_uuid']
            cu2 = self.mon.jobs[second['job_id']]['command_uuid']
            host._on_lifecycle({'command_uuid': cu1, 'state': 'started'})
            host._on_lifecycle({'command_uuid': cu2, 'state': 'started'})   # 工具轮之间被吸收
            host._on_result({'result': 'answer', 'session_id': 'fake-session',
                             'user_message_uuids': [cu1, cu2]})
            host._on_lifecycle({'command_uuid': cu1, 'state': 'completed'})
            host._on_lifecycle({'command_uuid': cu2, 'state': 'completed'})
        finally:
            stop()
        self.assertEqual(self.mon.jobs[first['job_id']]['status'], 'done')
        self.assertEqual(self.mon.jobs[second['job_id']]['status'], 'done')
        self.assertEqual([e for e in events if e[0] == 'hosted_turn_done'].__len__(), 1,
                         '同一回合吸收多条消息只应通知一次')
        self.assertEqual(host.turns, 1)

    def test_result_without_uuid_finishes_queued_jobs_in_order(self):
        host = monitor.Hosted(self.mon, str(self.root), 'existing')
        host.status = 'running'
        host._emit_session_event = lambda kind, data, transient=False: None
        _, first = host.send('one', None)
        _, second = host.send('two', None)
        first_job = self.mon.jobs[first['job_id']]
        second_job = self.mon.jobs[second['job_id']]
        with host.ready:
            for _, job, _ in host.pending:
                job['_claimed'] = True
                job['_written'] = True
            host.current = first_job
            first_job['status'] = 'running'
        # 极旧 CLI：result 不带 uuid；两轮结果应按提交顺序分别落位
        host._on_result({'result': 'r1'})
        self.assertTrue(first_job['_result_seen'])
        self.assertFalse(second_job.get('_result_seen'))
        host._finish_job(first_job, 'completed', notify=True)
        host._on_result({'result': 'r2'})
        self.assertTrue(second_job['_result_seen'])
        self.assertEqual(second_job['result'], 'r2')

    def test_host_cancel_delivered_queued_job_uses_control_request(self):
        host = monitor.Hosted(self.mon, str(self.root), 'existing')
        host.status = 'running'
        sender, written, stop = self._host_with_fake_stdin(host)
        try:
            _, first = host.send('first', None)
            _, second = host.send('second', None)
            deadline = time.time() + 2
            while time.time() < deadline and not (
                    sum(1 for w in written if w.get('type') == 'user') >= 2
                    and self.mon.jobs[second['job_id']].get('_written')):
                time.sleep(0.01)
            cu2 = self.mon.jobs[second['job_id']]['command_uuid']
            with patch.object(host, '_control', return_value={'cancelled': False}):
                self.assertEqual(self.mon.cancel_job('existing', second['job_id'])[0], 409)
            with patch.object(host, '_control', return_value={'cancelled': True}) as control:
                code, summary = self.mon.cancel_job('existing', second['job_id'])
            self.assertEqual(code, 200)
            self.assertEqual(summary['status'], 'cancelled')
            control.assert_called_once_with('cancel_async_message', timeout=10, message_uuid=cu2)
            self.assertIs(host.current, self.mon.jobs[first['job_id']])
            events = []
            host._emit_session_event = lambda kind, data, transient=False: events.append((kind, data))
            host._on_lifecycle({'command_uuid': cu2, 'state': 'cancelled'})
            self.assertFalse([e for e in events if e[0] == 'hosted_foreign_turn'],
                             '本地取消的消息不应再被当成外部回合')
        finally:
            stop()

    def test_turn_preempted_is_reported_as_superseded_not_failed(self):
        host = monitor.Hosted(self.mon, str(self.root), 'existing')
        host.status = 'running'
        events = []
        host._emit_session_event = lambda kind, data, transient=False: events.append((kind, data))
        _, first = host.send('first', None)
        job = self.mon.jobs[first['job_id']]
        with host.ready:
            host.current = job
            job['status'] = 'running'
        host._on_turn_preempted({'preempted_message_uuids': [job['command_uuid']],
                                 'preempted_by_uuid': 'follow-up'})
        host._on_lifecycle({'command_uuid': job['command_uuid'], 'state': 'cancelled'})
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['result'], '已被后续消息接管')
        done = [e for e in events if e[0] == 'hosted_turn_done']
        self.assertEqual(done[0][1]['status'], 'done')

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

    def test_oneshot_image_uses_stream_json_without_base64_in_job(self):
        stdout = '\n'.join([
            '{"type":"system","subtype":"init","session_id":"new-id"}',
            '{"type":"assistant","message":{"content":[]}}',
            '{"type":"result","session_id":"new-id","result":"ok"}',
        ])
        completed = subprocess.CompletedProcess([], 0, stdout, '')
        self.mon.claude_args = ['--output-format=json', '--input-format', 'text']
        with patch.object(core.subprocess, 'run', return_value=completed) as run:
            code, response = self.mon.new_session(str(self.root), '看图', images=[{
                'media_type': 'image/png', 'data': 'aW1hZ2U='}])
            self.assertEqual(code, 202)
            job = self.wait_done(response['job_id'])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index('--input-format') + 1], 'stream-json')
        self.assertEqual(cmd[cmd.index('--output-format') + 1], 'stream-json')
        self.assertIn('--verbose', cmd)
        self.assertEqual(cmd.count('--input-format'), 1)
        self.assertEqual(cmd.count('--output-format'), 1)
        self.assertNotIn('json', cmd)
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['message']['content'][0], {'type': 'text', 'text': '看图'})
        self.assertEqual(payload['message']['content'][1]['source']['data'], 'aW1hZ2U=')
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['result'], 'ok')
        self.assertEqual(job['result_session_id'], 'new-id')
        self.assertNotIn('aW1hZ2U=', job['text'])
        self.assertIn('[图片 1 张]', job['text'])

    def test_oneshot_stream_json_requires_result_event(self):
        completed = subprocess.CompletedProcess([], 0, '{"type":"system","subtype":"init"}', '')
        with patch.object(core.subprocess, 'run', return_value=completed):
            _, response = self.mon.new_session(str(self.root), '', images=[{
                'media_type': 'image/png', 'data': 'aW1hZ2U='}])
            job = self.wait_done(response['job_id'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('result', job['error'])

    def test_hosted_image_message_queues_content_blocks(self):
        from unittest.mock import Mock
        host = monitor.Hosted(self.mon, str(self.root), 'existing')
        host.status, host.proc = 'running', Mock()
        self.mon.hosted_by_sid['existing'] = host
        code, _ = self.mon.send_message('existing', '', images=[{
            'media_type': 'image/png', 'data': 'aW1hZ2U='}])
        self.assertEqual(code, 202)
        content = host.pending[0][0]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]['type'], 'image')
        self.assertEqual(content[0]['source']['media_type'], 'image/png')
        self.assertNotIn('aW1hZ2U=', host.pending[0][1]['text'])
        host.status = 'exited'

    def test_invalid_image_does_not_create_job(self):
        for image in ({'media_type': 'image/png', 'data': 'not base64!'},
                      {'media_type': [], 'data': 'aW1hZ2U='}):
            with self.subTest(image=image):
                code, response = self.mon.send_message('existing', 'hi', images=[image])
                self.assertEqual(code, 400)
                self.assertEqual(self.mon.jobs, {})

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

    def test_hosted_tracks_rate_limit_and_conversation_reset(self):
        host = hosted.Hosted(self.mon, str(self.root))
        host._on_rate_limit({"rate_limit_info": {
            "status": "rejected", "resets_at": 4102444800,
            "rate_limit_type": "five_hour", "utilization": 1.0}})
        self.assertEqual(host.last_rate_limit["status"], "rejected")
        self.assertEqual(host.info()["last_rate_limit"]["resets_at"], 4102444800)
        host.last_rate_limit["resets_at"] = 1
        self.assertIsNone(host.info()["last_rate_limit"])
        host._on_conversation_reset({"new_conversation_id": "new-conv"})
        self.assertEqual(host.last_conversation_reset["new_conversation_id"], "new-conv")
        self.assertEqual(host.info()["last_conversation_reset"]["new_conversation_id"],
                         "new-conv")

    def test_conversation_reset_switches_session_and_mappings(self):
        host = hosted.Hosted(self.mon, str(self.root), resume_id="original")
        host.session_id = "old"
        host.resume_id = "original"
        self.mon.hosted_by_sid["old"] = host
        self.mon.hosted_by_sid["original"] = host
        host._on_conversation_reset({"new_conversation_id": "fresh"})
        self.assertEqual(host.pending_conversation_reset["from"], "old")
        self.assertEqual(host.pending_conversation_reset["resume_from"], "original")
        # 同 session id / 子代理帧不能触发迁移
        host._maybe_switch_after_reset({"session_id": "old"})
        host._maybe_switch_after_reset({"session_id": "new",
                                        "parent_tool_use_id": "tool-1"})
        self.assertEqual(host.session_id, "old")
        self.assertIsNotNone(host.pending_conversation_reset)
        host._maybe_switch_after_reset({"type": "result", "session_id": "new"})
        self.assertEqual(host.session_id, "new")
        self.assertEqual(host.resume_id, "new")
        self.assertIs(self.mon.hosted_by_sid.get("new"), host)
        self.assertIsNone(self.mon.hosted_by_sid.get("old"))
        self.assertIsNone(self.mon.hosted_by_sid.get("original"))
        self.assertIsNone(host.pending_conversation_reset)
        self.assertEqual(host.last_conversation_reset["session_id"], "new")

    def test_terminate_process_closes_and_kills(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            hosted._terminate_process(proc, grace=0.2)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_hosted_effort_spawn_argument(self):
        host, job_id, captured = self.start_host_capturing(resume_id="existing", effort="high")
        self.assertIn("--resume=existing", captured[0])
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
        self.assertEqual(host.commands, ["compact", "fast", "model", "review"])

    def test_set_fast_requires_live_host(self):
        code, resp = self.mon.set_fast("existing", True)
        self.assertEqual(code, 409)
        self.assertIn("托管", resp["error"])

    def test_set_fast_roundtrip_tracks_state(self):
        host, job_id, _ = self.start_host_capturing(resume_id="existing")
        self.wait_done(job_id)
        # 首轮 init 未 opt-in：CLI 上报 off + sdk_opt_in_required
        deadline = time.time() + 2
        while host.fast_mode_reason is None and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(host.fast_mode_state, "off")
        self.assertEqual(host.fast_mode_reason, "sdk_opt_in_required")
        # 开启：先 opt-in 再发 /fast on，fake 的 result 带 fast_mode_state=on
        code, resp = self.mon.set_fast("existing", True)
        self.assertEqual(code, 202)
        self.assertTrue(host.fast_mode)
        job = self.wait_done(resp["job_id"])
        self.assertIn("Fast mode ON", job["result"])
        self.assertEqual(host.fast_mode_state, "on")
        self.assertIsNone(host.fast_mode_reason)
        # 关闭：/fast off 清 opt-in，fake 下一回合 init 回到 sdk_opt_in_required
        code, resp = self.mon.set_fast("existing", False)
        self.assertEqual(code, 202)
        self.assertFalse(host.fast_mode)
        job = self.wait_done(resp["job_id"])
        self.assertIn("Fast mode OFF", job["result"])

    def test_set_fast_control_failure_returns_409(self):
        host, _, _ = self.start_host_capturing(resume_id="existing")
        with patch.object(host, "_control",
                          side_effect=RuntimeError("控制请求失败: apply_flag_settings")):
            code, resp = self.mon.set_fast("existing", True)
        self.assertEqual(code, 409)
        self.assertIn("opt-in", resp["error"])
        self.assertIsNone(host.fast_mode)

    def test_set_fast_interrupted_before_execution_rolls_back(self):
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **kw):
            return original([sys.executable, fake] + cmd[1:], **kw)
        with patch.object(hosted.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), None, "interrupt-me")
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        time.sleep(0.6)      # 等 interrupt-me（5 秒）占住当前回合
        code, resp = self.mon.set_fast("fake-session", True)
        self.assertEqual(code, 202)
        self.assertTrue(host.fast_mode)
        # result 帧到达后 monitor 留 3 秒收集窗口，此时中断会清掉排队的 /fast on
        code, _ = host.interrupt()
        self.assertEqual(code, 200)
        self.assertIsNone(host.fast_mode, "命令未执行应回滚到切换前的值")

    def test_finish_job_settles_fast_intent_by_outcome(self):
        host = hosted.Hosted(self.mon, str(self.root))

        def make_job(result, **extra):
            return {"id": "j", "session": "existing", "status": "queued",
                    "started_at": 1, "command_uuid": "c1", "error": "",
                    "result": result, **extra}

        # CLI 拒绝（账号/模型不支持）：done 但 result 是 unavailable，回滚
        host.fast_mode = False
        host._finish_job(make_job("Fast mode unavailable: ...", _fast_prev=True),
                         "completed")
        self.assertTrue(host.fast_mode)
        # 正常执行：Fast mode ON，保留新意图
        host.fast_mode = True
        host._finish_job(make_job("Fast mode ON · model set to Opus", _fast_prev=False),
                         "completed")
        self.assertTrue(host.fast_mode)
        # 被后续消息接管：命令实际没跑，回滚
        host.fast_mode = True
        host._finish_job(make_job(None, _fast_prev=False, _preempted=True),
                         "completed")
        self.assertFalse(host.fast_mode)
        # 取消：回滚
        host.fast_mode = True
        host._finish_job(make_job(None, _fast_prev=False), "cancelled")
        self.assertFalse(host.fast_mode)
        # 无 fast 标记的普通 job 不受影响
        host.fast_mode = True
        host._finish_job(make_job("hello"), "cancelled")
        self.assertTrue(host.fast_mode)

    def test_update_fast_mode_ignores_and_validates(self):
        host = hosted.Hosted(self.mon, str(self.root))
        events = []
        host._emit_session_event = lambda kind, data: events.append(kind)
        # 不带字段的帧（assistant 等）不产生状态
        host._update_fast_mode({"type": "assistant"})
        self.assertIsNone(host.fast_mode_state)
        # 未知 state 归 None，reason 原样保留（开放集合）；每次变化各发一次事件
        host._update_fast_mode({"fast_mode_state": "warp", "fast_mode_disabled_reason": "brand_new"})
        self.assertIsNone(host.fast_mode_state)
        self.assertEqual(host.fast_mode_reason, "brand_new")
        host._update_fast_mode({"fast_mode_state": "off"})
        self.assertEqual(events, ["hosted_fast_mode", "hosted_fast_mode"])
        # 收敛到稳定值后，重复帧不重发事件
        host._update_fast_mode({"fast_mode_state": "off"})
        self.assertEqual(len(events), 2)
        # info() 投影
        info = host.info()
        self.assertEqual(info["fast_mode_state"], "off")
        self.assertIsNone(info["fast_mode_reason"])
        self.assertIsNone(info["fast_mode"])

    def test_hosted_stream_parts_reach_sse(self):
        # 托管进程的 thinking / tool_use 预览通过 transient SSE 进入网页
        sess = monitor.Session("fake-session", self.root / "fake.jsonl")
        sess.cwd = str(self.root)
        self.mon.sessions[str(sess.path)] = sess
        self.mon.first_scan_done = True
        q = queue.Queue()
        self.mon.subscribers.append(q)
        host, job_id = self.start_host(text="stream-parts")
        self.wait_done(job_id)
        events, deadline = [], time.time() + 3
        while time.time() < deadline:
            while True:
                try:
                    events.append(q.get_nowait())
                except queue.Empty:
                    break
            if any(e["type"] == "hosted_turn_done" for e in events):
                break
            time.sleep(0.02)
        self.mon.subscribers.remove(q)

        parts = [e["data"] for e in events if e["type"] == "stream_part"]
        thinking_done = [p for p in parts
                         if p["kind"] == "thinking" and p["phase"] == "done"]
        tool_done = [p for p in parts
                     if p["kind"] == "tool" and p["phase"] == "done"]
        self.assertTrue(thinking_done, "缺少 thinking 完成事件")
        self.assertEqual(thinking_done[0]["text"], "想一想")
        self.assertTrue(tool_done, "缺少 tool 完成事件")
        self.assertEqual(tool_done[0]["input"], {"command": "echo hi"})
        self.assertTrue(any(e["type"] == "hosted_turn_done" for e in events))
        self.assertEqual(host.stream_blocks, {}, "回合结束后预览块应清空")


if __name__ == "__main__":
    unittest.main()
