"""托管进程 stream-json 的 thinking / tool_use 流式预览。

这些事件只通过 transient SSE 推给网页，不进 recent；转录落盘后由前端对账移除。
"""
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
from claude_sessions import config, hosted


class HostedStreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.config_patch = patch.object(config, "CLAUDE_DIR", self.root / "config")
        self.config_patch.start()
        self.mon = monitor.Monitor(600)
        self.host = hosted.Hosted(self.mon, str(self.root))
        self.host.session_id = "sid"
        self.events = []
        self.host._emit_session_event = self._capture
        self.job = {"id": "job-1", "session": "sid", "started_at": 1}
        with self.host.ready:
            self.host.current = self.job
            self.host.stream_job = self.job

    def tearDown(self):
        self.config_patch.stop()
        self.tmp.cleanup()

    def _capture(self, kind, data, transient=False):
        self.events.append((kind, data, transient))

    def _stream(self, event, **extra):
        obj = {"type": "stream_event", "event": event}
        obj.update(extra)
        self.host._on_stream_event(obj)

    def _parts(self, phase=None):
        return [data for kind, data, _ in self.events
                if kind == "stream_part" and (phase is None or data["phase"] == phase)]

    def test_thinking_streams_cumulative_text_and_duration(self):
        clock = itertools.count(1.0, 0.1)
        with patch.object(hosted.util, "now_ts", lambda: next(clock)):
            self._stream({"type": "message_start",
                          "message": {"id": "msg-1"}})
            self._stream({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking", "thinking": ""}})
            for chunk in ("你", "好", "世界"):
                self._stream({"type": "content_block_delta", "index": 0,
                              "delta": {"type": "thinking_delta", "thinking": chunk}})
            self._stream({"type": "content_block_stop", "index": 0})

        starts = self._parts("start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["id"], "msg-1:0")
        self.assertEqual(starts[0]["kind"], "thinking")
        self.assertTrue(self.events[0][2], "流式事件必须是 transient")

        deltas = self._parts("delta")
        self.assertEqual([d["text"] for d in deltas], ["你", "你好", "你好世界"])

        done = self._parts("done")[0]
        self.assertEqual(done["text"], "你好世界")
        self.assertIsInstance(done["duration_ms"], int)
        self.assertEqual(self.host.stream_blocks["msg-1:0"]["done"], True)

        # done 之后的迟到增量不能恢复活动块
        self._stream({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "thinking_delta", "thinking": "！"}})
        self.assertEqual(self._parts("done")[0]["text"], "你好世界")

    def test_thinking_text_is_capped(self):
        with patch.object(hosted, "_STREAM_TEXT_LIMIT", 10):
            self._stream({"type": "message_start", "message": {"id": "msg-cap"}})
            self._stream({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking", "thinking": ""}})
            for chunk in ("1234567890", "abcdefghij"):
                self._stream({"type": "content_block_delta", "index": 0,
                              "delta": {"type": "thinking_delta",
                                        "thinking": chunk}})
            self._stream({"type": "content_block_stop", "index": 0})
        done = self._parts("done")[0]
        self.assertEqual(done["text"], "1234567890")
        self.assertTrue(done["truncated"])

    def test_repeated_index_without_message_id_starts_new_block(self):
        # 旧版 CLI 可能不发 message_start：同一 index 的第二段思考不能覆盖已结束的第一段
        for chunk in ("第一段", "第二段"):
            self._stream({"type": "content_block_start", "index": 0,
                          "content_block": {"type": "thinking", "thinking": ""}})
            self._stream({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "thinking_delta", "thinking": chunk}})
            self._stream({"type": "content_block_stop", "index": 0})
        done = self._parts("done")
        self.assertEqual([d["text"] for d in done], ["第一段", "第二段"])
        self.assertNotEqual(done[0]["id"], done[1]["id"])

    def test_tool_input_is_buffered_until_block_stop(self):
        self._stream({"type": "message_start", "message": {"id": "msg-2"}})
        self._stream({"type": "content_block_start", "index": 1,
                      "content_block": {"type": "tool_use", "id": "toolu_1",
                                        "name": "Bash", "input": {}}})
        for chunk in ('{"command"', ': "ls"}', ""):
            self._stream({"type": "content_block_delta", "index": 1,
                          "delta": {"type": "input_json_delta",
                                    "partial_json": chunk}})
        self._stream({"type": "content_block_stop", "index": 1})

        start = self._parts("start")[0]
        self.assertEqual(start["kind"], "tool")
        self.assertEqual(start["name"], "Bash")
        self.assertEqual(start["tool_use_id"], "toolu_1")
        # 参数生成期间不转发半截 JSON
        self.assertEqual(self._parts("delta"), [])

        done = self._parts("done")[0]
        self.assertEqual(done["id"], "toolu_1")
        self.assertEqual(done["input"], {"command": "ls"})
        self.assertTrue(done["input_ok"])
        self.assertFalse(done["truncated"])

    def test_malformed_input_does_not_raise(self):
        self._stream({"type": "message_start", "message": {"id": "msg-3"}})
        self._stream({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "tool_use", "id": "toolu_bad",
                                        "name": "Edit", "input": {}}})
        self._stream({"type": "content_block_delta", "index": 0,
                      "delta": {"type": "input_json_delta",
                                "partial_json": '{"file_path": '}})
        self._stream({"type": "content_block_stop", "index": 0})

        done = self._parts("done")[0]
        self.assertFalse(done["input_ok"])
        self.assertEqual(done["input"], {})

        # 畸形帧本身也不能抛异常（否则 stdout 读线程会死）
        self._stream(None)
        self._stream({"type": "content_block_delta", "index": 0, "delta": "bad"})
        self._stream({"type": "content_block_stop"})

    def test_subagent_and_stale_job_events_are_ignored(self):
        self._stream({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "thinking", "thinking": ""}},
                     parent_tool_use_id="agent-tool")
        self.assertEqual(self.events, [])

        with self.host.ready:
            self.host.current = None
        self._stream({"type": "content_block_start", "index": 1,
                      "content_block": {"type": "thinking", "thinking": ""}})
        self.assertEqual(self.events, [])

    def test_turn_cleanup_clears_preview_blocks(self):
        self._stream({"type": "message_start", "message": {"id": "msg-4"}})
        self._stream({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "thinking", "thinking": ""}})
        self.assertTrue(self.host.stream_blocks)

        self.host._on_conversation_reset({"new_conversation_id": "next"})
        self.assertEqual(self.host.stream_blocks, {})
        self.assertEqual(self.host.stream_index, {})

        # 重新建一个块，验证 _finish_job 也会清理
        with self.host.ready:
            self.host.current = self.job
            self.host.stream_job = self.job
        self._stream({"type": "content_block_start", "index": 0,
                      "content_block": {"type": "thinking", "thinking": ""}})
        self.assertTrue(self.host.stream_blocks)
        with patch.object(self.mon, "_terminal_job_locked"), \
                patch.object(self.mon, "queue_changed"):
            self.host._finish_job(self.job, "completed", notify=False)
        self.assertEqual(self.host.stream_blocks, {})


if __name__ == "__main__":
    unittest.main()