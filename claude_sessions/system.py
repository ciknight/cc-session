"""宿主机系统状态采样与电源操作。

测试通过 patch 本模块的 psutil / subprocess / 函数属性替换系统依赖，
使用处一律以 system.X 形式调用。
"""
import socket
import subprocess
import sys
import time

try:
    import psutil   # 系统面板采样用；未安装时 GET /system 返回 503
except ImportError:
    psutil = None


class SystemInfoUnavailable(Exception):
    """系统状态采集不可用（psutil 缺失等）。"""


def system_snapshot():
    """采集一次系统状态。cpu_percent 阻塞约 0.3 秒采样（首次调用可能更慢），
    ThreadingHTTPServer 每请求独立线程，不影响其他请求。"""
    if psutil is None:
        raise SystemInfoUnavailable("psutil 未安装")
    cores = psutil.cpu_percent(interval=0.3, percpu=True)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "hostname": socket.gethostname(),
        "uptime": max(0, int(time.time() - psutil.boot_time())),
        "cpu_percent": round(sum(cores) / len(cores), 1) if cores else 0.0,
        "cpu_cores": [round(c, 1) for c in cores],
        "mem_total": vm.total, "mem_used": vm.total - vm.available,
        "mem_available": vm.available, "mem_percent": vm.percent,
        "swap_total": swap.total, "swap_used": swap.used,
        "swap_percent": swap.percent,
    }


def power_command(action):
    """返回平台对应的电源命令参数列表（由调用方 Popen，不等待完成）；
    动作非法时抛 ValueError。Windows 重启带 /t 3 缓冲，让前端先收到响应。"""
    if action == "restart":
        if sys.platform == "win32":
            return ["shutdown", "/r", "/t", "3"]
        if sys.platform == "darwin":
            return ["osascript", "-e", 'tell app "System Events" to restart']
        return ["systemctl", "reboot"]
    if action == "sleep":
        if sys.platform == "win32":
            return ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        if sys.platform == "darwin":
            return ["osascript", "-e", 'tell app "System Events" to sleep']
        return ["systemctl", "suspend"]
    raise ValueError("action 必须是 restart 或 sleep")


def spawn_power(action):
    """Popen 电源命令（spawn 即返回，不等待完成）；OSError 由调用方处理。"""
    subprocess.Popen(power_command(action), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
