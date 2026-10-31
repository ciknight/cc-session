"""路径与可调常量。

测试通过 patch 本模块属性替换配置，因此使用处一律以 config.X 形式访问，
不要用 from config import X 把值绑进各自的模块命名空间。
"""
import os
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
# 包目录的上一级才是项目根（templates/ 与 static/ 所在）
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 首次打开大转录时只解析尾部窗口，向前翻页时再按块加载
MSG_TAIL_BYTES = 2 * 1024 * 1024
MSG_CHUNK_BYTES = 1 * 1024 * 1024

# 扫描缓存：保存每个转录已读到的 offset 和累计状态，重启时只解析新增字节。
# 子代理默认不扫描；开启全量扫描时缓存体积较大，降低落盘频率。
SCAN_CACHE_SAVE_SECONDS = 5
SCAN_CACHE_SAVE_SUBAGENTS_SECONDS = 30
SCAN_TAIL_SAMPLE_BYTES = 4096

# 非托管会话的一次性消息排队上限；超出后返回 429
REMOTE_QUEUE_LIMIT = 32

# 对话解析缓存最多保留的转录文件数（LRU）；最久未用的缓存连同解析锁一起淘汰
MSG_CACHE_SESSION_LIMIT = 8

# webhook 单 worker 的有界队列容量；满时丢弃新事件并限频记录
WEBHOOK_QUEUE_LIMIT = 1000
