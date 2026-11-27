import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_sessions import liveness


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, **fields):
        (self.sessions / name).write_text(
            json.dumps(fields, ensure_ascii=False), encoding="utf-8")

    def test_only_alive_pid_is_kept(self):
        self.write("111.json", pid=111, sessionId="alive", status="busy")
        self.write("222.json", pid=222, sessionId="dead", status="busy")
        result = liveness.read_cli_sessions(
            self.root, alive=lambda pid: pid == 111)
        self.assertEqual(sorted(result), ["alive"])
        self.assertEqual(result["alive"]["pid"], 111)

    def test_corrupt_and_incomplete_files_are_ignored(self):
        (self.sessions / "bad.json").write_text("{not json", encoding="utf-8")
        self.write("nopid.json", sessionId="no-pid")
        self.write("nosid.json", pid=333)
        self.write("ok.json", pid=333, sessionId="ok")
        result = liveness.read_cli_sessions(self.root, alive=lambda pid: True)
        self.assertEqual(list(result), ["ok"])

    def test_proc_start_mismatch_drops_reused_pid(self):
        self.write("111.json", pid=111, sessionId="old", procStart="123")
        self.write("222.json", pid=222, sessionId="new", procStart="456")

        def same_process(pid, proc_start):
            return pid == 222

        with patch.object(liveness, "_same_process", side_effect=same_process):
            result = liveness.read_cli_sessions(
                self.root, alive=lambda pid: True)
        self.assertEqual(list(result), ["new"])

    def test_missing_directory_returns_empty(self):
        self.assertEqual(liveness.read_cli_sessions(self.root / "absent",
                                                    alive=lambda pid: True), {})


if __name__ == "__main__":
    unittest.main()