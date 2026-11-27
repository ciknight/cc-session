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
# 单次轮询最多解析的行数与读取的字节数：超大转录首次回填时分批进行，
# 未读取的字节留在文件里（不占内存、不写缓存），避免长时间占住轮询线程。
# 字节上限是主要约束；行数上限只防极端小行，正常 4 MiB 内不会先触发行数上限。
SCAN_MAX_LINES_PER_TICK = 100_000
SCAN_MAX_BYTES_PER_TICK = 4 * 1024 * 1024
# 未完行的缓冲超过该大小时跳过本次缓存写入（保留旧条目）：
# 超大 base64 行不应让每个缓存周期都把几十 MB 编码落盘
SCAN_CACHE_BUF_LIMIT = 8 * 1024 * 1024

# 外部 CLI 存活探测：读 ~/.claude/sessions/<pid>.json（sessionId/pid），
# 校验进程仍存活。用于长工具执行时不被 --stale 误判，以及进程退出后的收尾。
LIVENESS_SCAN_SECONDS = 5
# 曾确认存活的进程消失后，working 会话在多少秒无转录事件后判定为 stale
LIVENESS_EXIT_GRACE = 30

# 网页图片消息：base64 在 JSON 中传输，单张/总量同时设限，避免请求与进程内存膨胀
IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_MAX_COUNT = 8
IMAGE_MAX_BYTES = 5 * 1024 * 1024 * 3 // 4  # base64 后不超过 API 的 5 MiB 上限
IMAGE_TOTAL_BYTES = 20 * 1024 * 1024
HTTP_IMAGE_BODY_LIMIT = 28 * 1024 * 1024

# 非托管会话的一次性消息排队上限；超出后返回 429
REMOTE_QUEUE_LIMIT = 32

# 文件在线预览：文本截断到该字符数（内容模式只读头部字节），图片超限拒绝
PREVIEW_TEXT_CHARS = 2_000_000
PREVIEW_IMAGE_LIMIT = 32 * 1024 * 1024

# 对话解析缓存最多保留的转录文件数（LRU）；同时限制估算 payload 总字节，
# 图片 base64 较大时优先淘汰最久未用且无活动请求的缓存
MSG_CACHE_SESSION_LIMIT = 8
MSG_CACHE_MAX_BYTES = 64 * 1024 * 1024

# webhook 单 worker 的有界队列容量；满时丢弃新事件并限频记录
WEBHOOK_QUEUE_LIMIT = 1000

# 临时探针：打印托管进程 stdout 的后台任务帧，确认 CLI 是否发送 task_updated 等事件
DEBUG_TASK_FRAMES = os.environ.get("CLAUDE_SESSIONS_DEBUG_TASK_FRAMES") == "1"

# 提示词模板：服务端全局共享，数量与单条长度设限，避免文件与浮层无限膨胀
PROMPT_TEMPLATE_LIMIT = 100
PROMPT_TEMPLATE_TITLE_CHARS = 80
PROMPT_TEMPLATE_TEXT_CHARS = 20_000
