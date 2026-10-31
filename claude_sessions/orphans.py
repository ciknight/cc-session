"""孤儿进程观察：托管进程退出后，它启动的后代（dev server / MCP server 等）可能仍在运行。

托管存活期间每 60 秒采样一次后代集合——只在退出时枚举不可靠：中间进程
死亡会断开 ppid 链，孙进程就漏掉了。托管退出后仍存活的后代列为残留。
PID 复用防护：所有存活判定都同时比对 create_time。
只读进程名（短名），不读命令行参数；列表在内存中，服务重启不恢复。
"""
import time

try:
    import psutil
except ImportError:
    psutil = None


class OrphanWatcher:
    def __init__(self):
        self._trees = {}    # hid -> {session_id, descendants: {pid: (create_time, name)}}
        self._orphans = {}  # pid -> {pid, name, create_time, hosted_id, session_id}

    def sample(self, hosted_map):
        """hosted_map: hid -> Hosted。存活的更新后代集合，退出的转化为残留列表。"""
        if psutil is None:
            return
        for hid, h in hosted_map.items():
            if h.status == "exited" or h.proc is None:
                continue
            tree = self._trees.setdefault(hid, {"session_id": h.session_id, "descendants": {}})
            tree["session_id"] = h.session_id
            try:
                root = psutil.Process(h.proc.pid)
                tree["descendants"] = {
                    p.pid: (p.create_time(), p.name())
                    for p in root.children(recursive=True)}
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
        for hid, tree in list(self._trees.items()):
            h = hosted_map.get(hid)
            if h is not None and h.status != "exited":
                continue
            for pid, (create_time, name) in tree["descendants"].items():
                if self._alive(pid, create_time) is not None:
                    self._orphans[pid] = {"pid": pid, "name": name,
                                          "create_time": create_time,
                                          "hosted_id": hid, "session_id": tree["session_id"]}
            del self._trees[hid]
        # 已自行退出的残留进程从列表移除
        for pid, info in list(self._orphans.items()):
            if self._alive(pid, info["create_time"]) is None:
                del self._orphans[pid]

    @staticmethod
    def _alive(pid, create_time):
        """进程存在且启动时间匹配（PID 复用不算同一进程）。"""
        if psutil is None:
            return None
        try:
            proc = psutil.Process(pid)
            if abs(proc.create_time() - create_time) > 0.01:
                return None
            if proc.status() == psutil.STATUS_ZOMBIE:
                return None
            return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return None

    def list(self):
        now = time.time()
        return [{"pid": i["pid"], "name": i["name"],
                 "age": max(0, int(now - i["create_time"])),
                 "hosted_id": i["hosted_id"], "session_id": i["session_id"]}
                for i in sorted(self._orphans.values(), key=lambda x: x["create_time"])]

    def end(self, pid):
        """发送 terminate（先经 create_time 校验）；已退出的顺带清理。返回是否发出终止。"""
        info = self._orphans.get(pid)
        if info is None:
            return False
        proc = self._alive(pid, info["create_time"])
        if proc is None:
            self._orphans.pop(pid, None)
            return False
        try:
            proc.terminate()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def end_all(self):
        return sum(1 for pid in list(self._orphans) if self.end(pid))
