import os
import unittest
from types import SimpleNamespace

from claude_sessions import orphans as orphans_mod
from claude_sessions.orphans import OrphanWatcher


class OrphanWatcherTests(unittest.TestCase):
    def setUp(self):
        if orphans_mod.psutil is None:
            self.skipTest("psutil 不可用")
        self.me = orphans_mod.psutil.Process(os.getpid())

    def hosted(self, status="running"):
        return SimpleNamespace(status=status,
                               proc=SimpleNamespace(pid=os.getpid()),
                               session_id="sid-1")

    def test_orphans_listed_after_hosted_exit(self):
        hosted = self.hosted()
        w = OrphanWatcher()
        w.sample({"h-1": hosted})
        # 注入一个“后代”：当前进程存活且 create_time 匹配
        w._trees["h-1"]["descendants"] = {
            os.getpid(): (self.me.create_time(), self.me.name())}
        hosted.status = "exited"
        w.sample({"h-1": hosted})
        listed = w.list()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["pid"], os.getpid())
        self.assertEqual(listed[0]["session_id"], "sid-1")
        self.assertGreaterEqual(listed[0]["age"], 0)

    def test_reused_pid_is_not_treated_as_same_process(self):
        hosted = self.hosted()
        w = OrphanWatcher()
        w.sample({"h-1": hosted})
        w._trees["h-1"]["descendants"] = {
            os.getpid(): (self.me.create_time() - 1000, self.me.name())}
        hosted.status = "exited"
        w.sample({"h-1": hosted})
        self.assertEqual(w.list(), [])   # create_time 不匹配：不算残留

    def test_end_skips_reused_pid_and_clears_entry(self):
        w = OrphanWatcher()
        w._orphans[os.getpid()] = {
            "pid": os.getpid(), "name": self.me.name(),
            "create_time": self.me.create_time() - 1000,
            "hosted_id": "h-1", "session_id": "sid-1"}
        # 绝不向当前进程发 terminate：create_time 不匹配时 _alive 返回 None
        self.assertFalse(w.end(os.getpid()))
        self.assertEqual(w.list(), [])


if __name__ == "__main__":
    unittest.main()
