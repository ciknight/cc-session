import unittest
from types import SimpleNamespace
from unittest.mock import patch

from claude_sessions import http_api, util


class Preferences:
    def __init__(self, value=None):
        self.value = value

    def get(self, sid):
        return self.value


def make_hosted(alive=True, process_model="proc-model", retry=None):
    return SimpleNamespace(
        alive=lambda: alive,
        process_model=process_model,
        last_api_retry=retry,
    )


class ApiProjectionTests(unittest.TestCase):
    def test_active_model_prefers_running_hosted_process(self):
        mon = SimpleNamespace(hosted_by_sid={"s": make_hosted()},
                              preferences=Preferences("selected"))
        self.assertEqual(http_api.active_model(mon, "s", "recent"), "proc-model")

    def test_active_model_falls_back_to_selection_then_recent(self):
        mon = SimpleNamespace(hosted_by_sid={}, preferences=Preferences("selected"))
        self.assertEqual(http_api.active_model(mon, "s", "recent"), "selected")
        mon.preferences = Preferences(None)
        self.assertEqual(http_api.active_model(mon, "s", "recent"), "recent")

    def test_merge_hosted_retry_adds_normalized_error(self):
        retry = {"attempt": 2, "max_retries": 10, "error_status": 503,
                 "error": {"message": "upstream busy"}, "ts": 1000}
        mon = SimpleNamespace(hosted_by_sid={"s": make_hosted(retry=retry)},
                              preferences=Preferences())
        detail = {"last_turn_end_at": None, "last_api_error": None}
        with patch.object(util, "now_ts", return_value=1001):
            http_api.merge_hosted_retry(mon, "s", detail)
        err = detail["last_api_error"]
        self.assertEqual(err["ts"], 1000)
        self.assertEqual(err["retry_attempt"], 2)
        self.assertEqual(err["kind"], "server")
        self.assertIn("upstream busy", err["text"])

    def test_newer_transcript_error_wins(self):
        retry = {"attempt": 1, "error_status": 503,
                 "error": {"message": "busy"}, "ts": 1000}
        mon = SimpleNamespace(hosted_by_sid={"s": make_hosted(retry=retry)},
                              preferences=Preferences())
        detail = {"last_turn_end_at": None,
                  "last_api_error": {"ts": 1002, "text": "transcript"}}
        with patch.object(util, "now_ts", return_value=1003):
            http_api.merge_hosted_retry(mon, "s", detail)
        self.assertEqual(detail["last_api_error"]["text"], "transcript")
        self.assertEqual(detail["last_api_error"]["ts"], 1002)

    def test_retry_after_turn_end_is_not_shown(self):
        retry = {"attempt": 1, "error_status": 503,
                 "error": {"message": "busy"}, "ts": 1000}
        mon = SimpleNamespace(hosted_by_sid={"s": make_hosted(retry=retry)},
                              preferences=Preferences())
        detail = {"last_turn_end_at": 1001, "last_api_error": None}
        with patch.object(util, "now_ts", return_value=1002):
            http_api.merge_hosted_retry(mon, "s", detail)
        self.assertIsNone(detail["last_api_error"])

    def test_stale_retry_and_exited_process_are_ignored(self):
        retry = {"attempt": 1, "error_status": 503,
                 "error": {"message": "busy"}, "ts": 1000}
        mon = SimpleNamespace(hosted_by_sid={"s": make_hosted(retry=retry)},
                              preferences=Preferences())
        detail = {"last_turn_end_at": None, "last_api_error": None}
        with patch.object(util, "now_ts", return_value=1400):
            http_api.merge_hosted_retry(mon, "s", detail)
        self.assertIsNone(detail["last_api_error"])

        mon.hosted_by_sid = {"s": make_hosted(alive=False, retry=retry)}
        with patch.object(util, "now_ts", return_value=1001):
            http_api.merge_hosted_retry(mon, "s", detail)
        self.assertIsNone(detail["last_api_error"])


if __name__ == "__main__":
    unittest.main()