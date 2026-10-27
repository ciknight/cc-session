import builtins
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_on_demand_subagent_uses_cache_before_main_scan(self):
        sub = self.proj / "parent-sid" / "subagents"
        sub.mkdir(parents=True)
        (self.proj / "parent-sid.jsonl").write_text(
            json.dumps(self.user_line("parent"), ensure_ascii=False) + "\n", encoding="utf-8")
        (sub / "agent-abc.jsonl").write_text(
            json.dumps(self.user_line("sub"), ensure_ascii=False) + "\n", encoding="utf-8")
        first = monitor.Monitor(600)
        first.scan_subagents = True
        first.scan_once()
        self.assertTrue(self.cache_path.is_file())

        calls = []
        original = monitor.Monitor.handle_line

        def counting(self, sess, line):
            calls.append(sess.id)
            return original(self, sess, line)

        with patch.object(monitor.Monitor, "handle_line", counting):
            second = monitor.Monitor(600)
            second.scan_subagents_once()
            third = monitor.Monitor(600)
            self.assertTrue(third.ensure_subagent("parent-sid::agent-abc"))
        self.assertEqual(calls, [])

    def test_truncate_to_zero_updates_cache(self):
        self.write([self.user_line("hello")])
        first = monitor.Monitor(600)
        first.scan_once()
        before = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertGreater(before["files"][str(self.transcript)]["offset"], 0)

        self.transcript.write_text("", encoding="utf-8")
        second = monitor.Monitor(600)
        second.scan_once()
        sess = second.by_sid("sid-1")
        self.assertEqual(sess.offset, 0)
        self.assertEqual(sess.user_msgs, 0)
        after = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(after["files"][str(self.transcript)]["offset"], 0)

    def test_empty_task_dir_clears_tasks(self):
        self.write([self.user_line("hello")])
        task_dir = self.root / "config" / "tasks" / "sid-1"
        task_dir.mkdir(parents=True)
        task_file = task_dir / "1.json"
        task_file.write_text(json.dumps({"id": "1", "subject": "任务", "status": "pending"},
                                        ensure_ascii=False), encoding="utf-8")
        mon = monitor.Monitor(600)
        mon.scan_once()
        self.assertEqual(mon.by_sid("sid-1").tasks["1"]["subject"], "任务")
        task_file.unlink()
        mon.scan_once()
        self.assertEqual(mon.by_sid("sid-1").tasks, {})

    def test_open_error_is_skipped(self):
        self.write([self.user_line("hello")])
        other = self.proj / "sid-2.jsonl"
        other.write_text(json.dumps(self.user_line("other"), ensure_ascii=False) + "\n",
                         encoding="utf-8")
        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path) == str(self.transcript):
                raise OSError("gone")
            return real_open(path, *args, **kwargs)

        mon = monitor.Monitor(600)
        with patch.object(monitor, "open", fake_open, create=True):
            mon.scan_once()   # 不应抛出
        self.assertEqual(mon.by_sid("sid-1").offset, 0)
        self.assertEqual(mon.by_sid("sid-2").user_msgs, 1)

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


class ScanLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(monitor, "CLAUDE_DIR", self.root / "config")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def assert_lock_free_during(self, mon, started):
        acquired = threading.Event()

        def grab():
            with mon.lock:
                acquired.set()

        threading.Thread(target=grab, daemon=True).start()
        self.assertTrue(acquired.wait(0.5), "磁盘读取阻塞时 mon.lock 应可获取")

    def test_transcript_read_does_not_hold_monitor_lock(self):
        mon = monitor.Monitor(300)
        gate = threading.Event()
        started = threading.Event()

        class BlockingFile:
            def seek(self, offset):
                pass

            def read(self, size=-1):
                started.set()
                gate.wait(5)
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        fake_path = SimpleNamespace(stat=lambda: SimpleNamespace(st_size=10))
        sess = monitor.Session("s1", fake_path)
        mon.sessions[str(fake_path)] = sess
        done = threading.Event()
        real_open = builtins.open

        def fake_open(file, *args, **kwargs):
            if file is fake_path:
                return BlockingFile()
            return real_open(file, *args, **kwargs)

        def run_scan():
            with patch.object(builtins, "open", fake_open):
                mon._scan_path(fake_path, "s1", None, False)
            done.set()

        threading.Thread(target=run_scan, daemon=True).start()
        self.assertTrue(started.wait(2))
        self.assert_lock_free_during(mon, started)
        gate.set()
        self.assertTrue(done.wait(2))

    def test_task_dir_read_does_not_hold_monitor_lock_and_merges(self):
        tasks = self.root / "config" / "tasks" / "s1"
        tasks.mkdir(parents=True)
        (tasks / "1.json").write_text(
            json.dumps({"id": 1, "subject": "t", "status": "pending"}),
            encoding="utf-8")
        mon = monitor.Monitor(300)
        sess = monitor.Session("s1", self.root / "config" / "projects" / "s1.jsonl")
        mon.sessions[str(sess.path)] = sess
        gate = threading.Event()
        started = threading.Event()
        real_read = Path.read_text

        def gated_read(self, *args, **kwargs):
            if self.name == "1.json":
                started.set()
                gate.wait(5)
                return json.dumps({"id": 1, "subject": "t", "status": "pending"})
            return real_read(self, *args, **kwargs)

        result = {}

        def run_merge():
            result["changed"] = mon._merge_task_dirs()
            result["done"] = True

        with patch.object(Path, "read_text", gated_read):
            thread = threading.Thread(target=run_merge, daemon=True)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assert_lock_free_during(mon, started)
            gate.set()
            thread.join(5)

        self.assertTrue(result.get("done"))
        self.assertTrue(result["changed"])
        self.assertEqual(sess.tasks, {"1": {"id": "1", "subject": "t",
                                            "description": "", "status": "pending"}})


if __name__ == "__main__":
    unittest.main()