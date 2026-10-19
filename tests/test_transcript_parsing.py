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


if __name__ == "__main__":
    unittest.main()