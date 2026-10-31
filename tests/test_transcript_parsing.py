import json
import unittest

import monitor


def line(obj):
    return json.dumps(obj, ensure_ascii=False)


class ParseMessageTests(unittest.TestCase):
    def test_edit_result_uses_structured_patch(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                     "content": "File updated"}]},
            "toolUseResult": {"structuredPatch": [
                {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
                 "lines": ["-old", "+new"]}]},
        }
        msg = monitor.parse_msg_line(line(obj))
        part = msg["parts"][0]
        self.assertEqual(part["type"], "tool_result")
        self.assertEqual(part["kind"], "diff")
        self.assertIn("-old", part["text"])
        self.assertIn("+new", part["text"])

    def test_bash_result_extracts_stdout_and_stderr(self):
        obj = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            "toolUseResult": {"stdout": "hello", "stderr": "warn"},
        }
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertIn("hello", part["text"])
        self.assertIn("warn", part["text"])

    def test_multiple_tool_results_in_one_line(self):
        obj = {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "b"}]},
        }
        parts = monitor.parse_msg_line(line(obj))["parts"]
        self.assertEqual([p["tool_use_id"] for p in parts], ["t1", "t2"])

    def test_meta_tool_note_is_marked(self):
        obj = {
            "type": "user",
            "isMeta": True,
            "sourceToolUseID": "t1",
            "message": {"content": [{"type": "text", "text": "skill body"}]},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["parts"][0]["type"], "tool_note")
        self.assertEqual(msg["parts"][0]["tool_use_id"], "t1")

    def test_boilerplate_meta_is_dropped(self):
        obj = {"type": "user", "isMeta": True,
               "message": {"content": [{"type": "text", "text": "boilerplate"}]}}
        self.assertIsNone(monitor.parse_msg_line(line(obj)))

    def test_tool_use_part_records_id(self):
        obj = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "a"}}]}}
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertEqual(part["tool_use_id"], "t1")

    def test_command_line_parses_to_command_part(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content":
                        "<command-name>/model</command-name>\n"
                        "            <command-message>model</command-message>\n"
                        "            <command-args>deepseek-v4.1-flash</command-args>"},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["role"], "system")
        self.assertEqual(len(msg["parts"]), 1)
        self.assertEqual(msg["parts"][0]["type"], "command")
        self.assertEqual(msg["parts"][0]["command"], "/model")
        self.assertEqual(msg["parts"][0]["args"], "deepseek-v4.1-flash")

    def test_command_line_without_args(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content":
                        "<command-name>/compact</command-name>\n"
                        "            <command-message>compact</command-message>"},
        }
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertEqual(part["command"], "/compact")
        self.assertEqual(part["args"], "")

    def test_local_command_stdout_parses_to_output_part(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content":
                        "<local-command-stdout>switched to model x</local-command-stdout>"},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["role"], "system")
        self.assertEqual(msg["parts"][0]["type"], "command_output")
        self.assertEqual(msg["parts"][0]["text"], "switched to model x")

    def test_other_system_text_unchanged(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["parts"][0]["type"], "text")


class SubagentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from claude_sessions import config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(config, "CLAUDE_DIR", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        proj = self.root / "projects" / "p1"
        sub = proj / "parent-sid" / "subagents"
        sub.mkdir(parents=True)
        (proj / "parent-sid.jsonl").write_text(
            json.dumps({"type": "user", "cwd": str(self.root),
                        "message": {"content": "hello"}}) + "\n", encoding="utf-8")
        (sub / "agent-abc.jsonl").write_text(
            json.dumps({"type": "user", "cwd": str(self.root),
                        "attributionAgent": "Explore",
                        "message": {"content": "sub task"}}) + "\n", encoding="utf-8")
        self.mon = monitor.Monitor(600)

    def test_subagent_is_lazy_loaded_and_hidden_by_default(self):
        self.mon.scan_once()
        main = self.mon.unique_sessions()
        self.assertEqual([s.id for s in main], ["parent-sid"])
        # 默认扫描不会解析子代理，显式 include_subagents 才会加载
        self.assertNotIn("parent-sid::agent-abc",
                         {s.id for s in self.mon.unique_sessions(include_subagents=True)})
        self.assertIn("parent-sid::agent-abc", self.mon.subagent_ids(self.mon.by_sid("parent-sid")))
        self.mon.scan_subagents_once()
        ids = {s.id for s in self.mon.unique_sessions(include_subagents=True)}
        self.assertIn("parent-sid::agent-abc", ids)
        sub = self.mon.by_sid("parent-sid::agent-abc")
        self.assertTrue(sub.is_subagent)
        self.assertEqual(sub.parent_id, "parent-sid")
        self.assertEqual(sub.agent_name, "Explore")

    def test_ensure_subagent_loads_single_file(self):
        self.mon.scan_once()
        self.assertIsNone(self.mon.by_sid("parent-sid::agent-abc"))
        self.assertTrue(self.mon.ensure_subagent("parent-sid::agent-abc"))
        self.assertEqual(self.mon.by_sid("parent-sid::agent-abc").parent_id, "parent-sid")

    def test_send_to_subagent_is_rejected(self):
        self.mon.scan_once()
        code, _ = self.mon.send_message("parent-sid::agent-abc", "hi")
        self.assertEqual(code, 400)

    def test_host_subagent_is_rejected(self):
        self.mon.scan_once()
        code, resp = self.mon.host_session(str(self.root), "parent-sid::agent-abc", "hi")
        self.assertEqual(code, 400)
        self.assertIn("子代理", resp["error"])


if __name__ == "__main__":
    unittest.main()