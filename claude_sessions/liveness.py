"""外部 Claude CLI 的存活探测。

交互式 CLI 会在 `~/.claude/sessions/<pid>.json` 里登记 sessionId、pid 与 cwd。
转录只能说明"最近写过什么"，长工具执行期间可能几分钟没有新行；进程表则能
确认会话是否仍在运行，避免把慢任务误判成 stale，也能在进程异常退出后尽快收尾。

只读探测：不做 fsnotify，由 Monitor 每隔几秒调用一次。
"""
import json
import os
from pathlib import Path

from . import config

try:
    import psutil
except ImportError:  # 系统面板可选依赖缺失时退回平台 API
    psutil = None


def _windows_pid_alive(pid):
    """Windows 上不能用 os.kill(pid, 0)（会 TerminateProcess），改用 OpenProcess。"""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                          # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _same_process(pid, proc_start):
    """Windows 上尽力用进程创建时间排除 PID 复用。

    注册表里的 procStart 是 FILETIME ticks（1601 起 100ns）。只有在
    psutil 可用且字段可解析时才校验；任何不确定都返回 True，避免误杀。"""
    if psutil is None or os.name != "nt" or not proc_start:
        return True
    try:
        expected = int(proc_start) / 10_000_000 - 11_644_473_600
        actual = psutil.Process(pid).create_time()
        return abs(actual - expected) <= 10
    except Exception:
        return True


def pid_alive(pid):
    """判断 pid 是否仍对应一个存活进程（PID 复用不在本层处理）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:
            return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def read_cli_sessions(claude_dir=None, alive=None):
    """返回 {sessionId: record}，只保留宿主进程仍存活的登记项。

    文件可能在进程异常退出后残留，因此 PID 校验是必须的；无法读取/解析的
    文件一律跳过，探测失败不能影响会话扫描本身。
    """
    root = Path(claude_dir or config.CLAUDE_DIR) / "sessions"
    alive = pid_alive if alive is None else alive
    sessions = {}
    try:
        paths = list(root.glob("*.json"))
    except OSError:
        return sessions
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        sid = record.get("sessionId")
        pid = record.get("pid")
        if not isinstance(sid, str) or not sid:
            continue
        if not isinstance(pid, int) or pid <= 0 or not alive(pid):
            continue
        if not _same_process(pid, record.get("procStart")):
            continue
        record["_file"] = path.name
        sessions[sid] = record
    return sessions