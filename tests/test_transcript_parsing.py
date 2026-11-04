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

    def test_tool_use_part_carries_structured_input_and_summary(self):
        obj = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit",
             "input": {"file_path": "a.py", "old_string": "old", "new_string": "new"}},
            {"type": "tool_use", "id": "t2", "name": "Bash",
             "input": {"command": "pytest -q", "description": "run tests"}}]}}
        edit, bash = monitor.parse_msg_line(line(obj))["parts"]
        self.assertEqual(edit["input_data"]["file_path"], "a.py")
        self.assertEqual(edit["summary"], "a.py")
        # 大字段不进入展示 payload
        self.assertNotIn("old_string", edit["input_data"])
        self.assertEqual(bash["input_data"]["command"], "pytest -q")
        self.assertEqual(bash["summary"], "pytest -q")

    def test_diff_result_reports_stats_and_path(self):
        obj = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                     "content": "File updated"}]},
            "toolUseResult": {"filePath": "src/a.py", "structuredPatch": [
                {"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 3,
                 "lines": ["-old", "+new", "+extra"]}]},
        }
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertEqual(part["stats"], {"added": 2, "removed": 1})
        self.assertEqual(part["file_path"], "src/a.py")

    def test_async_agent_result_reports_agent_id(self):
        obj = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                     "content": "Async agent launched successfully.\n"
                                                "agentId: a2109fb7412787143"}]},
            "toolUseResult": {"isAsync": True},
        }
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertEqual(part["agent_id"], "a2109fb7412787143")

    def test_thinking_block_is_preserved(self):
        obj = {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "先确认目录结构"},
            {"type": "text", "text": "开始处理"}]}}
        parts = monitor.parse_msg_line(line(obj))["parts"]
        self.assertEqual(parts[0]["type"], "thinking")
        self.assertEqual(parts[0]["text"], "先确认目录结构")
        self.assertEqual(parts[1]["type"], "text")

    def test_compact_boundary_and_summary_are_parsed(self):
        boundary = {
            "type": "system", "subtype": "compact_boundary",
            "timestamp": "2026-01-01T00:00:00Z",
            "content": "Conversation compacted",
            "compactMetadata": {"trigger": "manual", "preTokens": 511375,
                                "durationMs": 158241},
        }
        msg = monitor.parse_msg_line(line(boundary))
        self.assertEqual(msg["role"], "system")
        part = msg["parts"][0]
        self.assertEqual(part["type"], "compact")
        self.assertEqual(part["trigger"], "manual")
        self.assertEqual(part["pre_tokens"], 511375)
        self.assertEqual(part["duration_ms"], 158241)

        summary = monitor.parse_msg_line(line({
            "type": "user", "isCompactSummary": True,
            "message": {"content": [{"type": "text", "text": "压缩后的摘要"}]},
        }))
        self.assertEqual(summary["parts"][0]["type"], "compact_summary")
        self.assertEqual(summary["parts"][0]["text"], "压缩后的摘要")

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

    def test_user_and_tool_images_keep_base64_blocks(self):
        source = {"type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}
        user = monitor.parse_msg_line(line({
            "type": "user", "message": {"content": [
                {"type": "text", "text": "看图"}, {"type": "image", "source": source}]}}))
        image = user["parts"][1]
        self.assertEqual({k: image[k] for k in ("type", "media_type", "data")}, {
            "type": "image", "media_type": "image/png", "data": "aW1hZ2U="})
        self.assertEqual(len(image["image_id"]), 64)
        result = monitor.parse_msg_line(line({
            "type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "read-1",
                "content": [{"type": "image", "source": source}]}]},
            "toolUseResult": {"type": "image", "file": {
                "base64": "aW1hZ2U=", "type": "image/png"}}}))["parts"][0]
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["text"], "图片")
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["data"], "aW1hZ2U=")
        self.assertEqual(result["images"][0]["image_id"], image["image_id"])

    def test_tool_image_content_ignores_non_dict_blocks(self):
        source = {"type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}
        result = monitor.parse_msg_line(line({
            "type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "read-1",
                "content": [None, "ignored", {"type": "image", "source": source}]}]}}))["parts"][0]
        self.assertEqual(result["kind"], "image")
        self.assertEqual(len(result["images"]), 1)

    def test_line_level_image_without_path_keeps_placeholder(self):
        result = monitor.parse_msg_line(line({
            "type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "read-1", "content": ""}]},
            "toolUseResult": {"type": "image"}}))["parts"][0]
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["text"], "图片")

    def test_external_or_svg_images_are_not_rendered(self):
        for source in ({"type": "url", "url": "https://invalid/x.svg"},
                       {"type": "base64", "media_type": [], "data": "aW1hZ2U="}):
            with self.subTest(source=source):
                msg = monitor.parse_msg_line(line({
                    "type": "user", "message": {"content": [{
                        "type": "image", "source": source}]}}))
                self.assertIsNone(msg)

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