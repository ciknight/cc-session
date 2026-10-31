import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import monitor
from claude_sessions import util


class SessionStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sess = monitor.Session("sid-1", Path(self.temp.name) / "session.jsonl")
        self.mon = monitor.Monitor(stale_after=300)
        self.now = 1_700_000_000
        self.clock = patch.object(util, "now_ts", return_value=self.now)
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

    def test_turn_finished_event_includes_duration(self):
        events = []
        self.mon.first_scan_done = True
        self.mon.emit = lambda sess, kind, data, transient=False: events.append((kind, data))
        self.line("user", "hello")
        self.sess.last_input_ts = self.now - 42
        self.line("assistant", [{"type": "text", "text": "done"}], stop_reason="end_turn")
        finished = [d for k, d in events if k == "turn_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["duration"], 42)

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

    def test_subagent_entry_running_then_done(self):
        self.line("assistant", [{
            "type": "tool_use", "id": "a1", "name": "Agent",
            "input": {"description": "scan code", "subagent_type": "Explore"},
        }], stop_reason="tool_use")
        sub = self.sess.subagents[0]
        self.assertEqual(sub["tid"], "a1")
        self.assertEqual(sub["state"], "running")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 1)

        self.line("user", [{"type": "tool_result", "tool_use_id": "a1", "content": "ok"}])
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 0)

    def test_interrupted_agent_collected_at_turn_duration(self):
        # 回合被中断时不会有 tool_result：turn_duration 权威结束时把运行中条目收尾
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "scan"}}], stop_reason="tool_use")
        self.mon.handle_line(self.sess, json.dumps({
            "type": "system", "subtype": "turn_duration", "durationMs": 1234,
            "timestamp": self.timestamp(self.now),
        }))
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 0)

    def test_async_agent_launch_result_keeps_running(self):
        # 后台异步 Agent 的 tool_result 只是"已启动"确认（行级 toolUseResult.isAsync），
        # 真正的完成信号是后续 task-notification；期间条目保持 running、会话保持 working
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "bg"}}], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "launched"}],
                  toolUseResult={"isAsync": True, "status": "async_launched"})
        self.assertEqual(self.sess.subagents[0]["state"], "running")
        self.assertTrue(self.sess.subagents[0].get("async"))
        self.assertIn("a1", self.sess.active_agents)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 1)

    def test_task_notification_finishes_async_agent(self):
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "bg"}}], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "launched"}],
                  toolUseResult={"isAsync": True})
        self.line("user", [{"type": "text", "text":
                            "<task-notification>\n<task-id>x</task-id>\n"
                            "<tool-use-id>a1</tool-use-id>\n<status>completed</status>\n"
                            "</task-notification>"}])
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertNotIn("a1", self.sess.active_agents)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 0)

    def test_turn_duration_sweep_skips_async_agent(self):
        # 异步子代理跨回合运行：主回合 turn_duration 不能把它当中断残留收尾
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "bg"}}], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "launched"}],
                  toolUseResult={"isAsync": True})
        self.mon.handle_line(self.sess, json.dumps({
            "type": "system", "subtype": "turn_duration", "durationMs": 1234,
            "timestamp": self.timestamp(self.now),
        }))
        self.assertEqual(self.sess.subagents[0]["state"], "running")
        self.line("user", [{"type": "text", "text":
                            "<task-notification>\n<tool-use-id>a1</tool-use-id>\n"
                            "</task-notification>"}])
        self.assertEqual(self.sess.subagents[0]["state"], "done")

    def test_interrupted_agent_active_set_cleared_at_turn_duration(self):
        # 中断的同步 Agent 不会再有 tool_result：turn_duration 清扫条目的同时
        # 必须收敛 active_agents，否则 _set_idle 永不触发、会话永久 working
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "scan"}}], stop_reason="tool_use")
        for _ in range(2):
            self.mon.handle_line(self.sess, json.dumps({
                "type": "system", "subtype": "turn_duration", "durationMs": 1234,
                "timestamp": self.timestamp(self.now),
            }))
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertEqual(self.sess.active_agents, set())
        self.assertEqual(self.sess.state, "idle")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_async_agent_survives_turn_duration_in_active_set(self):
        # 异步子代理跨回合运行：turn_duration 后仍留在 active_agents 保持 working，
        # 直到 task-notification 才解除；随后回合结束可正常回到 idle
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "bg"}}], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "launched"}],
                  toolUseResult={"isAsync": True})
        self.mon.handle_line(self.sess, json.dumps({
            "type": "system", "subtype": "turn_duration", "durationMs": 1234,
            "timestamp": self.timestamp(self.now),
        }))
        self.assertIn("a1", self.sess.active_agents)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "working")
        self.line("user", [{"type": "text", "text":
                            "<task-notification>\n<tool-use-id>a1</tool-use-id>\n"
                            "</task-notification>"}])
        self.assertEqual(self.sess.active_agents, set())
        self.mon.handle_line(self.sess, json.dumps({
            "type": "system", "subtype": "turn_duration", "durationMs": 1234,
            "timestamp": self.timestamp(self.now),
        }))
        self.assertEqual(self.sess.state, "idle")

    def test_stale_session_downgrades_sync_running_subagent_in_output(self):
        # 父进程异常退出会留下截断转录：tool_result/turn_duration 都不会再来。
        # 输出层把会话已停滞时的非 async 运行条目降级为 stale，累计状态不动
        stale_ts = self.now - self.mon.stale_after - 1
        self.line("assistant", [{
            "type": "tool_use", "id": "a1", "name": "Agent",
            "input": {"description": "scan", "subagent_type": "Explore"},
        }], timestamp=self.timestamp(stale_ts), stop_reason="tool_use")
        s = self.sess.summary(self.mon.stale_after)
        self.assertEqual(s["state"], "stale")
        self.assertEqual(s["subagents_running"], 0)
        detail = self.sess.detail(self.mon.stale_after)
        self.assertEqual(detail["subagents"][0]["state"], "stale")
        # 内部累计状态保持 running，会话恢复活跃时不误报 done
        self.assertEqual(self.sess.subagents[0]["state"], "running")

    def test_stale_output_keeps_async_subagent_running(self):
        # 异步子代理没有回合寿命：父会话停滞时仍可能在后台运行，不降级
        stale_ts = self.now - self.mon.stale_after - 1
        self.line("assistant", [{
            "type": "tool_use", "id": "a1", "name": "Agent",
            "input": {"description": "bg"}},
        ], timestamp=self.timestamp(stale_ts), stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "launched"}],
                  timestamp=self.timestamp(stale_ts),
                  toolUseResult={"isAsync": True})
        s = self.sess.summary(self.mon.stale_after)
        self.assertEqual(s["state"], "stale")
        self.assertEqual(s["subagents_running"], 1)
        detail = self.sess.detail(self.mon.stale_after)
        self.assertEqual(detail["subagents"][0]["state"], "running")

    def test_task_notification_in_queue_operation_line_finishes_async_agent(self):
        # 新版 CLI 把 task-notification 写成 queue-operation 行（content 为字符串），
        # 而不是 user 消息；同样要按 tool-use-id 配对收尾异步子代理
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "bg"}}], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "launched"}],
                  toolUseResult={"isAsync": True})
        self.mon.handle_line(self.sess, json.dumps({
            "type": "queue-operation", "operation": "enqueue",
            "content": "<task-notification>\n<task-id>x</task-id>\n"
                       "<tool-use-id>a1</tool-use-id>\n<status>completed</status>\n"
                       "</task-notification>",
            "timestamp": self.timestamp(self.now),
        }))
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertNotIn("a1", self.sess.active_agents)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 0)

    def test_subagent_entry_error_on_error_result(self):
        self.line("assistant", [{
            "type": "tool_use", "id": "a1", "name": "Agent",
            "input": {"description": "scan"},
        }], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "boom", "is_error": True}])
        self.assertEqual(self.sess.subagents[0]["state"], "error")

    def test_detail_orders_running_subagents_first(self):
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "first"}}], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1", "content": "ok"}])
        self.line("assistant", [{"type": "tool_use", "id": "a2", "name": "Agent",
                                 "input": {"description": "second"}}], stop_reason="tool_use")
        subs = self.sess.detail(self.mon.stale_after)["subagents"]
        self.assertEqual([s["description"] for s in subs], ["second", "first"])

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

    def test_sdk_and_typed_prompt_sources_are_real_input(self):
        for i, source in enumerate(("sdk", "typed", "queued")):
            self.line("user", [{"type": "text", "text": "real"}],
                      promptSource=source, timestamp=self.timestamp(self.now + i))
            self.assertEqual(self.sess.last_input_ts, self.now + i)
        self.line("user", [{"type": "text", "text": "system"}],
                  promptSource="system", timestamp=self.timestamp(self.now + 10))
        self.assertEqual(self.sess.last_input_ts, self.now + 2, "system 注入不应推进 last_input")

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

    def test_api_retry_is_recorded_and_cleared_by_reply(self):
        # 上游 503 时 CLI 只写 system/api_error，不写 assistant，网页需要能看到原因
        self.line("system", [], subtype="api_error",
                  error={"message": "503 model temporarily unavailable"},
                  retryAttempt=3, maxRetries=10, retryInMs=2302)
        self.assertEqual(self.sess.state, "working")
        err = self.sess.summary(self.mon.stale_after)["last_api_error"]
        self.assertEqual(err["text"], "503 model temporarily unavailable")
        self.assertEqual(err["retry_attempt"], 3)
        self.assertEqual(err["max_retries"], 10)
        self.assertEqual(err["retry_in_ms"], 2302)

        self.line("assistant", [{"type": "text", "text": "好了"}])
        self.assertIsNone(self.sess.last_api_error)

    def test_api_retry_cleared_by_new_user_message(self):
        self.line("system", [], subtype="api_error",
                  error={"message": "503 busy"}, retryAttempt=1)
        self.line("user", [{"type": "text", "text": "再试一次"}])
        self.assertIsNone(self.sess.last_api_error)

    def test_api_retry_cleared_by_interrupt_marker(self):
        self.line("system", [], subtype="api_error",
                  error={"message": "503 busy"}, retryAttempt=7)
        self.line("user", [{"type": "text", "text": "[Request interrupted by user]"}])
        self.assertIsNone(self.sess.last_api_error)

    def test_restore_old_subagent_entries_marked_done(self):
        self.sess.restore_cache({
            "subagents": [{"type": "Explore", "description": "d", "ts": 1},
                          {"type": "claude", "description": "e", "ts": 2,
                           "tid": "t2", "state": "running"}],
        })
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertEqual(self.sess.subagents[0]["tid"], "")
        # 新格式条目原样保留（运行中的子任务在增量读取中会等来 tool_result）
        self.assertEqual(self.sess.subagents[1]["state"], "running")
        self.assertEqual(self.sess.subagents[1]["tid"], "t2")


if __name__ == "__main__":
    unittest.main()
