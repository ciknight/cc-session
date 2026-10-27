import json
import queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import monitor


class ManagementAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.config_patch = patch.object(monitor, "CLAUDE_DIR", self.root / ".claude")
        self.config_patch.start()
        self.mon = monitor.Monitor(600)
        self.sess = monitor.Session("sid-1", self.project / "session.jsonl")
        self.sess.cwd = str(self.project)
        self.sess.model = "actual-model"
        self.mon.sessions[str(self.sess.path)] = self.sess
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), monitor.make_handler(self.mon, 3))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.config_patch.stop()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        body = None if data is None else json.dumps(data).encode()
        req = Request(self.base + path, data=body, headers=headers or {}, method="GET" if data is None else "POST")
        return urlopen(req)

    def snapshot(self, sid=None):
        response = urlopen(self.base + '/events' + ('?session=' + sid if sid else ''), timeout=5)
        try:
            while True:
                line = response.readline().decode()
                if line.startswith('data:'):
                    data = json.loads(line[5:])['data']
                    return [data['session']] if sid else data['sessions']
        finally:
            response.close()

    def test_metadata_is_consistent_across_http_and_sse_and_preserves_transcript(self):
        self._write_transcript(1)
        original = self.sess.path.read_bytes()
        self.mon.first_scan_done = True
        events = queue.Queue()
        self.mon.subscribers.append(events)
        headers = {'Content-Type': 'application/json'}
        result = json.load(self.request('/sessions/sid-1/metadata',
            {'alias': '  自定义  ', 'pinned': True, 'archived': True}, headers))
        fields = {'alias': '自定义', 'pinned': True, 'archived': True, 'metadata_revision': 1}
        self.assertEqual(result, {'id': 'sid-1', **fields})
        for item in (json.load(self.request('/sessions'))[0],
                     json.load(self.request('/sessions?full=1'))[0],
                     json.load(self.request('/sessions/sid-1')),
                     self.snapshot()[0], self.snapshot('sid-1')[0]):
            self.assertEqual({k: item[k] for k in fields}, fields)
        self.assertEqual(events.get_nowait()['type'], 'session_metadata_changed')
        self.assertEqual(self.sess.path.read_bytes(), original)
        self.assertEqual(json.load(self.request('/sessions/sid-1/messages'))['total'], 1)

    def test_management_write_validation_and_queue_without_transcript(self):
        host = monitor.Hosted(self.mon, str(self.project), 'unscanned')
        host.status = 'running'
        _, response = host.send('queued', 'opus')
        jid = response['job_id']
        path = '/sessions/unscanned/jobs/' + jid + '/cancel'
        for headers, code in (({}, 415), ({'Content-Type': 'application/json', 'Origin': 'http://evil.invalid'}, 403)):
            with self.assertRaises(HTTPError) as error:
                self.request(path, {}, headers)
            self.assertEqual(error.exception.code, code)
        result = json.load(self.request('/sessions/unscanned/jobs'))
        self.assertEqual(set(result), {'session', 'running', 'queued', 'recent'})
        self.assertTrue(result['queued'][0]['cancellable'])
        headers = {'Content-Type': 'application/json'}
        self.assertEqual(json.load(self.request(path, {}, headers))['status'], 'cancelled')
        self.assertEqual(json.load(self.request(path, {}, headers))['status'], 'cancelled')
        self.assertNotIn('_owner', json.load(self.request('/jobs/' + jid)))
        for patch_body in ({}, {'revision': 2}, {'pinned': 1}):
            with self.assertRaises(HTTPError) as error:
                self.request('/sessions/sid-1/metadata', patch_body, headers)
            self.assertEqual(error.exception.code, 400)
        with patch.object(self.mon.metadata, 'update', side_effect=ValueError('disk failed')):
            with self.assertRaises(HTTPError) as error:
                self.request('/sessions/sid-1/metadata', {'archived': True}, headers)
            self.assertEqual(error.exception.code, 500)

    def test_management_unknown_subagent_and_terminal_status_codes(self):
        headers = {'Content-Type': 'application/json'}
        child = monitor.Session('parent::agent-child', self.project / 'child.jsonl')
        child.is_subagent = True
        self.mon.sessions[str(child.path)] = child
        job = self.mon._new_job('sid-1', 'done')
        with self.mon.lock:
            self.mon._terminal_job_locked(job, 'done')
        cases = [
            ('/sessions/missing/jobs', None, {}, 404),
            ('/sessions/sid-1/jobs/missing/cancel', {}, headers, 404),
            ('/sessions/other/jobs/' + job['id'] + '/cancel', {}, headers, 404),
            ('/sessions/sid-1/jobs/' + job['id'] + '/cancel', {}, headers, 409),
            ('/sessions/parent::agent-child/jobs/' + job['id'] + '/cancel', {}, headers, 400),
            ('/sessions/parent::agent-child/metadata', {'alias': 'x'}, headers, 400),
            ('/sessions/missing/metadata', {'alias': 'x'}, headers, 404),
            ('/sessions/sid-1/metadata', {'alias': 'x'}, {}, 415),
            ('/sessions/sid-1/metadata', {'alias': 'x'}, {**headers, 'Origin': 'http://evil.invalid'}, 403),
        ]
        for path, body, request_headers, expected in cases:
            with self.subTest(path=path, expected=expected):
                with self.assertRaises(HTTPError) as error:
                    self.request(path, body, request_headers)
                self.assertEqual(error.exception.code, expected)

    def test_model_api_persists_selection_separately_from_actual_model(self):
        response = self.request("/sessions/sid-1/model", {"model": "custom/model"},
                                {"Content-Type": "application/json"})
        self.assertEqual(json.load(response)["selected_model"], "custom/model")
        detail = json.load(self.request("/sessions/sid-1"))
        self.assertEqual(detail["model"], "actual-model")
        self.assertEqual(detail["selected_model"], "custom/model")

    def test_model_api_rejects_cross_origin_and_bad_model(self):
        with self.assertRaises(HTTPError) as cross:
            self.request("/sessions/sid-1/model", {"model": "opus"},
                         {"Content-Type": "application/json", "Origin": "http://evil.invalid"})
        self.assertEqual(cross.exception.code, 403)
        with self.assertRaises(HTTPError) as invalid:
            self.request("/sessions/sid-1/model", {"model": []}, {"Content-Type": "application/json"})
        self.assertEqual(invalid.exception.code, 400)

    def test_execution_writes_reject_cross_site_before_delegation(self):
        cases = (("/new", "new_session"), ("/hosted", "host_session"),
                 ("/sessions/sid-1/message", "send_message"))
        for path, method in cases:
            with self.subTest(path=path), patch.object(
                    self.mon, method, return_value=(202, {})) as call:
                with self.assertRaises(HTTPError) as error:
                    self.request(path, {"text": "hello", "cwd": str(self.project)},
                                 {"Content-Type": "text/plain",
                                  "Origin": "http://evil.invalid"})
                self.assertEqual(error.exception.code, 403)
                call.assert_not_called()

    def test_hosted_delete_rejects_cross_site_before_stopping(self):
        hosted = SimpleNamespace(stop=lambda *a: None,
                                 info=lambda: {"hosted_id": "h1"})
        self.mon.hosted["h1"] = hosted
        with patch.object(hosted, "stop") as stop:
            req = Request(self.base + "/hosted/h1", method="DELETE",
                          headers={"Origin": "http://evil.invalid"})
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 403)
            stop.assert_not_called()

    def test_static_rejects_sibling_directory_with_same_prefix(self):
        static = self.root / "static"
        static.mkdir()
        secret = self.root / "static-secret" / "secret.txt"
        secret.parent.mkdir()
        secret.write_text("not-public", encoding="utf-8")
        with patch.object(monitor, "STATIC_DIR", static):
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as sock:
                sock.sendall(b"GET /static/../static-secret/secret.txt HTTP/1.0\r\n\r\n")
                chunks = []
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
        response = b"".join(chunks)
        self.assertIn(b" 404 ", response.split(b"\r\n", 1)[0])
        self.assertNotIn(b"not-public", response)

    def test_malformed_request_version_returns_400_without_crashing_logger(self):
        # 请求行版本号非法时 stdlib 只设置 command(None)，没有 path；
        # log_request 不应因此崩溃，客户端应收到 400 错误页而不是连接被掐断。
        # 此时 request_version 回退为 HTTP/0.9，stdlib 不发状态行/头，只有 body
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as sock:
            sock.sendall(b"GET / HTTP/9x.9\r\n\r\n")
            data = sock.recv(4096)
        self.assertIn(b"Error code: 400", data)
        self.assertIn(b"Bad request version", data)

    def test_spa_route_serves_index_html(self):
        for path in ("/", "/p/demo%5Cproject", "/pending"):
            response = self.request(path)
            self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertIn("Claude Sessions", response.read().decode("utf-8"))

    def test_cc_switch_api_restricts_unknown_cwd(self):
        with patch.object(self.mon.cc_switch, "status") as status:
            with self.assertRaises(HTTPError) as error:
                self.request("/cc-switch?cwd=" + str(self.root))
        self.assertEqual(error.exception.code, 400)
        status.assert_not_called()

    def test_cc_switch_status_only_called_for_known_cwd(self):
        with patch.object(self.mon.cc_switch, "status", return_value={"profiles": ["safe"]}) as status:
            result = json.load(self.request("/cc-switch?cwd=" + str(self.project)))
        self.assertEqual(result["profiles"], ["safe"])
        status.assert_called_once_with(str(self.project))

    def test_grouped_project_root_is_valid_for_profile_operation(self):
        child = self.project / "web"
        child.mkdir()
        child_session = monitor.Session("child", child / "child.jsonl")
        child_session.cwd = str(child)
        self.mon.sessions[str(child_session.path)] = child_session
        self.assertTrue(self.mon.known_project_cwd(str(self.project)))

    def test_profile_apply_checks_same_origin_and_delegates_confirmed_revision(self):
        result = {"current_profile": "safe", "target_cwd": str(self.project), "backed_up": True}
        with patch.object(self.mon.cc_switch, "apply", return_value=result) as apply:
            response = self.request("/cc-switch/local", {
                "cwd": str(self.project), "profile": "safe", "confirm": True, "revision": "rev1",
            }, {"Content-Type": "application/json", "Origin": self.base})
        self.assertEqual(json.load(response), result)
        apply.assert_called_once_with(str(self.project), "safe", "rev1", confirm=True)

    def test_profile_apply_failure_returns_safe_error_and_logs_exit_code(self):
        from cc_switch import CCSwitch
        import io
        import subprocess

        home = self.root / "home"
        profiles = home / ".cc-switch" / "profiles"
        profiles.mkdir(parents=True)
        (profiles / "safe.json").write_text("{}", encoding="utf-8")
        self.mon.cc_switch = CCSwitch(home / ".claude", home=home)
        output = io.StringIO()
        with (patch("cc_switch.shutil.which", return_value="cc-switch"),
              patch("cc_switch.subprocess.run", return_value=subprocess.CompletedProcess(
                  [], 3, stdout="secret-stdout", stderr="secret-stderr")),
              patch("sys.stdout", output)):
            with self.assertRaises(HTTPError) as error:
                self.request("/cc-switch/local", {
                    "cwd": str(self.project), "profile": "safe", "confirm": True, "revision": "absent",
                }, {"Content-Type": "application/json"})
            result = json.load(error.exception)
        self.assertEqual(error.exception.code, 500)
        self.assertEqual(set(result), {"error"})
        self.assertIn("退出码=3", output.getvalue())
        self.assertNotIn("secret", json.dumps(result) + output.getvalue())

    def test_profile_apply_rejects_cross_origin_before_delegation(self):
        with patch.object(self.mon.cc_switch, "apply") as apply:
            with self.assertRaises(HTTPError) as error:
                self.request("/cc-switch/local", {
                    "cwd": str(self.project), "profile": "safe", "confirm": True, "revision": "rev1",
                }, {"Content-Type": "application/json", "Origin": "http://evil.invalid"})
        self.assertEqual(error.exception.code, 403)
        apply.assert_not_called()

    def wait_for(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def _write_transcript(self, count):
        lines = []
        for i in range(count):
            lines.append(json.dumps({
                "type": "user", "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": f"m{i}"}}))
        path = self.project / "sid-1.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.sess.path = path

    def test_sessions_include_subagents_triggers_lazy_scan(self):
        with patch.object(self.mon, "scan_subagents_once") as scan:
            response = self.request("/sessions?include_subagents=1")
        scan.assert_called_once()
        self.assertEqual([item["id"] for item in json.load(response)], ["sid-1"])

    def test_messages_paging_default_and_before(self):
        self._write_transcript(250)
        first = json.load(self.request("/sessions/sid-1/messages"))
        self.assertEqual(first["total"], 250)
        self.assertEqual(len(first["messages"]), 100)
        self.assertEqual((first["start"], first["end"], first["has_more"]),
                         (150, 250, True))
        older = json.load(self.request("/sessions/sid-1/messages?before=150&limit=50"))
        self.assertEqual((older["start"], older["end"]), (100, 150))
        self.assertEqual(older["messages"][0]["parts"][0]["text"], "m100")
        newer = json.load(self.request("/sessions/sid-1/messages?from=240"))
        self.assertEqual([m["parts"][0]["text"] for m in newer["messages"]],
                         [f"m{i}" for i in range(240, 250)])

    def test_messages_tail_window_loads_earlier_chunks(self):
        self._write_transcript(250)
        with patch.object(monitor, "MSG_TAIL_BYTES", 500), \
                patch.object(monitor, "MSG_CHUNK_BYTES", 2000):
            page = json.load(self.request("/sessions/sid-1/messages?limit=100"))
            messages = list(page["messages"])
            self.assertLess(page["total"], 250)
            self.assertTrue(page["has_more"])
            for _ in range(100):
                if not page["has_more"]:
                    break
                page = json.load(self.request(
                    f"/sessions/sid-1/messages?before={page['start']}&limit=100"))
                messages = page["messages"] + messages
            else:
                self.fail("分页循环未结束")
        self.assertEqual(len(messages), 250)
        self.assertEqual(messages[0]["parts"][0]["text"], "m0")
        self.assertEqual(messages[-1]["parts"][0]["text"], "m249")

    def test_messages_paging_rejects_bad_params(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/sessions/sid-1/messages?before=1&from=2")
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/sessions/sid-1/messages?limit=0")
        self.assertEqual(error.exception.code, 400)

    def test_emit_transient_does_not_touch_recent_or_webhook(self):
        self.mon.first_scan_done = True
        self.mon.webhook = "http://invalid.example/hook"
        q = queue.Queue()
        self.mon.subscribers.append(q)
        self.mon.emit(self.sess, "turn_delta", {"text": "hi"}, transient=True)
        self.assertEqual(list(self.sess.recent), [])
        event = q.get_nowait()
        self.assertEqual(event["type"], "turn_delta")
        self.assertEqual(event["seq"], 1)

    def test_sse_connection_starts_with_snapshot(self):
        response = urlopen(self.base + "/events", timeout=5)
        lines = []
        try:
            while True:
                line = response.readline().decode().rstrip("\n")
                lines.append(line)
                if line == "" and any(x.startswith("data:") for x in lines):
                    break
        finally:
            response.close()
        self.assertIn("event: snapshot", lines)
        data = json.loads([x for x in lines if x.startswith("data:")][0][5:])
        self.assertIn("sessions", data["data"])
        self.assertIn("interactions", data["data"])
        self.assertIsInstance(data["seq"], int)

    def test_choice_requires_all_answers(self):
        hosted = type("H", (), {"hid": None, "session_id": "sid-1"})()
        inter = monitor.Interaction("c1", hosted, {"tool_name": "AskUserQuestion"},
                                    kind="choice",
                                    questions=[{"question": "Q1"}])
        self.mon.interactions["c1"] = inter
        code, _ = self.mon.respond_interaction("c1", {"behavior": "allow"})
        self.assertEqual(code, 400)
        code, _ = self.mon.respond_interaction(
            "c1", {"behavior": "allow", "answers": {"Q1": "A"}})
        self.assertEqual(code, 200)
        self.assertEqual(inter.answers, {"Q1": "A"})

    def test_hook_client_round_trip(self):
        self.mon.http_port = self.server.server_port
        self.mon.hook_token = "tok"
        result = subprocess.run(
            self.mon.hook_command(), shell=True,
            input=json.dumps({"tool_name": "Bash", "note": "中文"}, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_hook_requires_token(self):
        self.mon.hook_token = "tok"
        with self.assertRaises(HTTPError) as error:
            self.request("/hook/PreToolUse", {"tool_name": "AskUserQuestion"},
                         {"Content-Type": "application/json"})
        self.assertEqual(error.exception.code, 401)

    def test_hook_non_question_returns_empty(self):
        self.mon.hook_token = "tok"
        result = json.load(self.request(
            "/hook/PreToolUse", {"tool_name": "Bash"},
            {"Content-Type": "application/json", "X-Monitor-Token": "tok"}))
        self.assertEqual(result, {})

    def test_hook_blocks_until_answered(self):
        self.mon.hook_token = "tok"
        payload = {"session_id": "sid-1", "tool_use_id": "t1",
                   "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": [
                       {"question": "Q1", "options": [{"label": "A"}]}]}}
        box = {}

        def call():
            box["resp"] = json.load(self.request(
                "/hook/PreToolUse", payload,
                {"Content-Type": "application/json", "X-Monitor-Token": "tok"}))

        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(self.wait_for(
            lambda: self.mon.pending_interactions("sid-1")))
        inter = self.mon.pending_interactions("sid-1")[0]
        self.assertEqual(inter["kind"], "choice")
        code, _ = self.mon.respond_interaction(
            inter["id"], {"behavior": "allow", "answers": {"Q1": "A"}})
        self.assertEqual(code, 200)
        thread.join(timeout=5)
        self.assertEqual(
            box["resp"]["hookSpecificOutput"]["updatedInput"]["answers"],
            {"Q1": "A"})

    def test_interactions_endpoint(self):
        hosted = type("H", (), {"hid": "h1", "session_id": "sid-1"})()
        inter = monitor.Interaction("r1", hosted, {
            "tool_name": "Bash", "tool_use_id": "t1", "input": {"command": "ls"}})
        self.mon.interactions["r1"] = inter
        result = json.load(self.request("/interactions?session=sid-1"))
        self.assertEqual(result["interactions"][0]["tool_name"], "Bash")
        self.assertEqual(result["interactions"][0]["preview"]["kind"], "text")

    def test_default_approval_is_auto_but_saved_mode_wins(self):
        detail = json.load(self.request("/sessions/sid-1"))
        self.assertEqual(detail["approval"], "auto")
        self.mon.approvals.set("sid-1", "ask")
        detail = json.load(self.request("/sessions/sid-1"))
        self.assertEqual(detail["approval"], "ask")

    def test_approval_endpoint_persists(self):
        response = self.request("/sessions/sid-1/approval", {"approval": "full"},
                                {"Content-Type": "application/json"})
        self.assertEqual(json.load(response)["approval"], "full")
        self.assertEqual(self.mon.approvals.get("sid-1"), "full")
        detail = json.load(self.request("/sessions/sid-1"))
        self.assertEqual(detail["approval"], "full")

    def test_interrupt_non_hosted_returns_409(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/sessions/sid-1/interrupt", {},
                         {"Content-Type": "application/json"})
        self.assertEqual(error.exception.code, 409)

    def test_live_approval_switch_failure_does_not_persist(self):
        class FakeHost:
            def alive(self):
                return True

            def set_approval(self, mode):
                raise RuntimeError("切换失败")

        self.mon.hosted_by_sid["sid-1"] = FakeHost()
        with patch.object(self.mon.approvals, "set") as save:
            code, _ = self.mon.set_approval("sid-1", "auto")
        self.assertEqual(code, 409)
        save.assert_not_called()

    def test_live_approval_switch_persists_after_success(self):
        calls = []

        class FakeHost:
            def alive(self):
                return True

            def set_approval(self, mode):
                calls.append(("host", mode))

        self.mon.hosted_by_sid["sid-1"] = FakeHost()
        real_set = self.mon.approvals.set
        def record(sid, mode):
            calls.append(("disk", mode))
            return real_set(sid, mode)
        with patch.object(self.mon.approvals, "set", side_effect=record):
            code, _ = self.mon.set_approval("sid-1", "full")
        self.assertEqual(code, 200)
        self.assertEqual(calls, [("host", "full"), ("disk", "full")])

    def test_respond_cross_origin_rejected(self):
        hosted = type("H", (), {"hid": "h1", "session_id": "sid-1"})()
        inter = monitor.Interaction("r1", hosted, {"tool_name": "Bash", "input": {}})
        self.mon.interactions["r1"] = inter
        with self.assertRaises(HTTPError) as error:
            self.request("/interactions/r1/respond", {"behavior": "allow"},
                         {"Content-Type": "application/json", "Origin": "http://evil.invalid"})
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(inter.state, "pending")

    def test_effort_endpoint_persists_and_validates(self):
        headers = {'Content-Type': 'application/json'}
        result = json.load(self.request('/sessions/sid-1/effort',
                                        {'effort': 'high'}, headers))
        self.assertEqual(result, {'effort': 'high'})
        self.assertEqual(self.mon.efforts.get('sid-1'), 'high')
        detail = json.load(self.request('/sessions/sid-1'))
        self.assertEqual(detail['effort'], 'high')
        self.assertEqual(detail['commands'], [])
        try:
            self.request('/sessions/sid-1/effort', {'effort': 'ultra'}, headers)
            self.fail('应返回 400')
        except HTTPError as e:
            self.assertEqual(e.code, 400)
        # default 清除
        result = json.load(self.request('/sessions/sid-1/effort',
                                        {'effort': 'default'}, headers))
        self.assertEqual(result, {'effort': 'default'})
        self.assertIsNone(self.mon.efforts.get('sid-1'))


class SSEDisconnectTests(unittest.TestCase):
    def test_client_abort_does_not_escape_handler(self):
        # Windows 上客户端断开抛 ConnectionAbortedError(10053)，不应冒泡成 traceback
        mon = monitor.Monitor(600)
        handler = monitor.make_handler(mon, 3)

        class AbortingWfile:
            def write(self, data):
                raise ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接")

            def flush(self):
                pass

        fake = SimpleNamespace(
            wfile=AbortingWfile(),
            send_response=lambda *a: None,
            send_header=lambda *a: None,
            end_headers=lambda *a: None,
            _snapshot=lambda only: {"seq": 1, "session": None, "type": "snapshot", "data": {}},
        )
        handler._sse(fake, None)   # 不应抛出
        self.assertEqual(mon.subscribers, [])


if __name__ == "__main__":
    unittest.main()
