import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


def _ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


class ScanCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(monitor, "CLAUDE_DIR", self.root / "config")
        self.patch.start()
        self.proj = self.root / "config" / "projects" / "p1"
        self.proj.mkdir(parents=True)
        self.transcript = self.proj / "sid-1.jsonl"
        self.cache_path = self.root / "config" / "claude-sessions" / "scan-cache.json"

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def user_line(text, epoch=1_700_000_000):
        return {"type": "user", "timestamp": _ts(epoch),
                "message": {"content": text}}

    @staticmethod
    def assistant_line(text, message_id, input_tokens, output_tokens, epoch=1_700_000_001):
        return {"type": "assistant", "timestamp": _ts(epoch),
                "message": {"id": message_id, "content": [{"type": "text", "text": text}],
                            "usage": {"input_tokens": input_tokens,
                                      "output_tokens": output_tokens,
                                      "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0}}}

    def write(self, lines):
        self.transcript.write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines),
            encoding="utf-8")

    def append(self, line):
        with self.transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def scan_with_count(self, mon):
        calls = []
        original = monitor.Monitor.handle_line

        def counting(self, sess, line):
            calls.append(line)
            return original(self, sess, line)

        with patch.object(monitor.Monitor, "handle_line", counting):
            mon.scan_once()
        return calls

    def test_restart_restores_state_without_reparsing(self):
        self.write([self.user_line("hello"),
                    self.assistant_line("hi", "m1", 10, 2)])
        first = monitor.Monitor(600)
        first.scan_once()
        self.assertTrue(self.cache_path.is_file())

        second = monitor.Monitor(600)
        calls = self.scan_with_count(second)
        self.assertEqual(calls, [])
        sess = second.by_sid("sid-1")
        self.assertEqual(sess.user_msgs, 1)
        self.assertEqual(sess.assistant_msgs, 1)
        self.assertEqual(sess.tokens["input"], 10)
        self.assertEqual(sess.last_user, "hello")
        self.assertEqual(sess.last_assistant, "hi")

    def test_append_parses_only_new_bytes(self):
        self.write([self.user_line("hello"),
                    self.assistant_line("hi", "m1", 10, 2)])
        monitor.Monitor(600).scan_once()

        self.append(self.user_line("again", epoch=1_700_000_002))
        self.append(self.assistant_line("ok", "m2", 20, 4, epoch=1_700_000_003))
        second = monitor.Monitor(600)
        calls = self.scan_with_count(second)
        self.assertEqual(len(calls), 2)
        sess = second.by_sid("sid-1")
        self.assertEqual(sess.user_msgs, 2)
        self.assertEqual(sess.assistant_msgs, 2)
        self.assertEqual(sess.tokens["input"], 30)
        self.assertEqual(sess.tokens["output"], 6)

    def test_truncate_resets_derived_state(self):
        self.write([self.user_line("one", 1_700_000_000),
                    self.user_line("two", 1_700_000_001)])
        monitor.Monitor(600).scan_once()

        self.write([self.user_line("new", 1_700_000_002)])
        second = monitor.Monitor(600)
        calls = self.scan_with_count(second)
        self.assertEqual(len(calls), 1)
        sess = second.by_sid("sid-1")
        self.assertEqual(sess.user_msgs, 1)
        self.assertEqual(sess.last_user, "new")

    def test_same_size_rewrite_is_reparsed(self):
        self.write([self.user_line("aaaa")])
        monitor.Monitor(600).scan_once()

        self.write([self.user_line("bbbb")])
        second = monitor.Monitor(600)
        calls = self.scan_with_count(second)
        self.assertEqual(len(calls), 1)
        sess = second.by_sid("sid-1")
        self.assertEqual(sess.user_msgs, 1)
        self.assertEqual(sess.last_user, "bbbb")

    def test_corrupt_cache_falls_back_to_full_scan(self):
        self.write([self.user_line("hello")])
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text("{broken", encoding="utf-8")

        mon = monitor.Monitor(600)
        calls = self.scan_with_count(mon)
        self.assertEqual(len(calls), 1)
        self.assertEqual(mon.by_sid("sid-1").user_msgs, 1)
        # 扫描完成后用有效内容覆盖损坏缓存
        self.assertIsInstance(json.loads(self.cache_path.read_text(encoding="utf-8")), dict)


if __name__ == "__main__":
    unittest.main()