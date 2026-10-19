import json
import queue
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
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

    def _write_transcript(self, count):
        lines = []
        for i in range(count):
            lines.append(json.dumps({
                "type": "user", "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": f"m{i}"}}))
        path = self.project / "sid-1.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.sess.path = path

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

    def test_interactions_endpoint(self):
        hosted = type("H", (), {"hid": "h1", "session_id": "sid-1"})()
        inter = monitor.Interaction("r1", hosted, {
            "tool_name": "Bash", "tool_use_id": "t1", "input": {"command": "ls"}})
        self.mon.interactions["r1"] = inter
        result = json.load(self.request("/interactions?session=sid-1"))
        self.assertEqual(result["interactions"][0]["tool_name"], "Bash")
        self.assertEqual(result["interactions"][0]["preview"]["kind"], "text")

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


if __name__ == "__main__":
    unittest.main()
