import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import monitor


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sess = monitor.Session("sid-1", Path(self.temp.name) / "session.jsonl")
        self.mon = monitor.Monitor(stale_after=300)
        self.now = 1_700_000_000
        self.clock = patch.object(monitor, "now_ts", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.temp.cleanup)

    def line(self, typ, content, timestamp=None, stop_reason=None, usage=None, **fields):
        message = {"content": content}
        if stop_reason is not None:
            message["stop_reason"] = stop_reason
        if usage is not None:
            message["usage"] = usage
        obj = {
            "type": typ,
            "timestamp": timestamp or self.timestamp(self.now),
            "message": message,
            **fields,
        }
        self.mon.handle_line(self.sess, json.dumps(obj))

    @staticmethod
    def timestamp(epoch):
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")

    def start_agent_and_finish_main_turn(self, agent_id="agent-call-1"):
        self.line("assistant", [{
            "type": "tool_use",
            "id": agent_id,
            "name": "Agent",
            "input": {"description": "Run a subagent"},
        }], stop_reason="tool_use")
        self.line("assistant", [{"type": "text", "text": "Done"}], stop_reason="end_turn")

    def test_active_agent_keeps_session_working_after_main_turn_ends(self):
        self.start_agent_and_finish_main_turn()

        self.assertEqual(self.sess.state, "idle")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "working")

    def test_matching_agent_result_allows_idle_after_main_turn_ends(self):
        self.start_agent_and_finish_main_turn()
        self.line("user", [{"type": "tool_result", "tool_use_id": "agent-call-1", "content": "Finished"}])
        self.line("assistant", [{"type": "text", "text": "All done"}], stop_reason="end_turn")

        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_unmatched_tool_result_does_not_clear_active_agent(self):
        self.start_agent_and_finish_main_turn()
        self.line("user", [{"type": "tool_result", "tool_use_id": "other-call", "content": "Finished"}])
        self.line("assistant", [{"type": "text", "text": "Still waiting"}], stop_reason="end_turn")

        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "working")

    def test_error_tool_result_clears_matching_active_agent(self):
        self.start_agent_and_finish_main_turn()
        self.line("user", [{
            "type": "tool_result",
            "tool_use_id": "agent-call-1",
            "content": "Agent failed",
            "is_error": True,
        }])
        self.line("assistant", [{"type": "text", "text": "Agent failure handled"}], stop_reason="end_turn")

        self.assertNotIn("agent-call-1", self.sess.active_agents)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_stale_state_takes_precedence_over_active_agent(self):
        stale_ts = self.now - self.mon.stale_after - 1
        self.line("assistant", [{
            "type": "tool_use",
            "id": "agent-call-1",
            "name": "Agent",
            "input": {"description": "Run a subagent"},
        }], timestamp=self.timestamp(stale_ts), stop_reason="tool_use")

        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "stale")

    def test_stale_threshold_is_strictly_greater_than_elapsed_time(self):
        boundary_ts = self.now - self.mon.stale_after
        self.line("assistant", [{
            "type": "tool_use",
            "id": "agent-call-1",
            "name": "Agent",
            "input": {"description": "Run a subagent"},
        }], timestamp=self.timestamp(boundary_ts), stop_reason="tool_use")

        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "working")

    def test_detail_lists_subagents_newest_first_without_reordering_internal_list(self):
        older = {"type": "general-purpose", "description": "older", "ts": 100}
        newer = {"type": "general-purpose", "description": "newer", "ts": 200}
        self.sess.subagents.extend([older, newer])

        detail = self.sess.detail(self.mon.stale_after)

        self.assertEqual([agent["ts"] for agent in detail["subagents"]], [200, 100])
        self.assertEqual([agent["ts"] for agent in self.sess.subagents], [100, 200])

    def test_turn_duration_wins_over_end_turn(self):
        self.line("user", [{"type": "text", "text": "do it"}])
        self.line("assistant", [{"type": "text", "text": "working"}], stop_reason="end_turn")
        self.line("system", [], subtype="turn_duration")
        self.assertTrue(self.sess.saw_turn_duration)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_end_turn_is_fallback_without_turn_duration(self):
        self.line("user", [{"type": "text", "text": "hi"}])
        self.line("assistant", [{"type": "text", "text": "done"}], stop_reason="end_turn")
        self.assertFalse(self.sess.saw_turn_duration)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_tool_result_and_meta_do_not_advance_last_input(self):
        self.line("user", [{"type": "text", "text": "real input"}], timestamp=self.timestamp(self.now))
        self.assertEqual(self.sess.last_input_ts, self.now)
        later = self.now + 10
        self.line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
                  timestamp=self.timestamp(later))
        self.line("user", [{"type": "text", "text": "injected"}], isMeta=True,
                  timestamp=self.timestamp(later + 10))
        self.assertEqual(self.sess.last_input_ts, self.now)

    def test_injected_prompt_does_not_advance_last_input(self):
        self.line("user", [{"type": "text", "text": "injected"}],
                  origin={"kind": "coordinator"}, timestamp=self.timestamp(self.now))
        self.assertIsNone(self.sess.last_input_ts)

    def test_pending_tool_keeps_working_after_end_turn(self):
        self.line("user", [{"type": "text", "text": "go"}])
        self.line("assistant", [{"type": "tool_use", "id": "tool-1", "name": "Bash",
                                 "input": {"command": "sleep 1"}}], stop_reason="tool_use")
        self.line("assistant", [{"type": "text", "text": "waiting"}], stop_reason="end_turn")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "working")
        self.line("user", [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}])
        self.line("system", [], subtype="turn_duration")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_context_tokens_use_latest_usage_not_sum(self):
        self.line("assistant", [{"type": "text", "text": "a"}], usage={
            "input_tokens": 10, "output_tokens": 2,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4})
        self.assertEqual(self.sess.context_tokens, 19)
        self.line("assistant", [{"type": "text", "text": "b"}], usage={
            "input_tokens": 5, "output_tokens": 1})
        self.assertEqual(self.sess.context_tokens, 6)
        self.assertEqual(self.sess.tokens["input"], 15)

    def test_meta_source_tool_is_not_user_input(self):
        self.line("user", [{"type": "text", "text": "skill body"}],
                  isMeta=True, sourceToolUseID="tool-9", timestamp=self.timestamp(self.now))
        self.assertIsNone(self.sess.last_input_ts)
        self.assertEqual(self.sess.user_msgs, 0)

    def test_boilerplate_meta_is_ignored(self):
        self.line("user", [{"type": "text", "text": "system boilerplate"}],
                  isMeta=True, timestamp=self.timestamp(self.now))
        self.assertIsNone(self.sess.last_input_ts)

    def test_interrupt_marker_is_system(self):
        self.line("user", [{"type": "text", "text": "[Request interrupted by user]"}])
        self.assertIsNone(self.sess.last_input_ts)
        self.assertEqual(self.sess.user_msgs, 0)


if __name__ == "__main__":
    unittest.main()
