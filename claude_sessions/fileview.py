"""文件预览的分类与文本解码纯函数（不触 HTTP、不做路径安全裁决）。"""
from pathlib import Path

# markdown 单独成类：预览页渲染而非纯文本
MD_EXTS = {".md", ".markdown"}
# 图片直显；svg 不在 image——按文本展示源码，避免同源脚本执行
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}
TEXT_EXTS = {
    ".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".tsv", ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".less", ".xml", ".svg", ".sh", ".bash",
    ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".rs", ".go", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".vue",
    ".svelte", ".diff", ".patch", ".env", ".gitignore", ".dockerignore",
    ".editorconfig", ".lock", ".mod", ".sum",
}
# 常见无扩展名文本文件（小写全名比较）
TEXT_NAMES = {"makefile", "dockerfile", "license", "readme", "notice", "changes"}

# 内容模式下最多读取的字节数：utf-8 最坏 4 字节/字符，200 万字符封顶足够
TEXT_HEAD_BYTES = 8_000_100
# 内容嗅探窗口：判断未知扩展名是否文本
SNIFF_BYTES = 8192


def classify_file(path: Path) -> str:
    """按扩展名/文件名/内容嗅探分类：markdown | text | image | binary。"""
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in MD_EXTS:
        return "markdown"
    if suffix in IMAGE_EXTS:
        return "image"
    if suffix in TEXT_EXTS or name in TEXT_NAMES:
        return "text"
    # 未知扩展名或无扩展名：前几 KiB 含 NUL 字节视为二进制
    try:
        with open(path, "rb") as f:
            head = f.read(SNIFF_BYTES)
    except OSError:
        return "binary"
    return "binary" if b"\x00" in head else "text"


def decode_text(data: bytes) -> str:
    """BOM/UTF-16 启发 → utf-8 严格 → gb18030 严格 → utf-8 替换。

    中文 Windows 文本常见 GBK 编码；含 NUL 字节的"文本"几乎必是 UTF-16
    （Windows 记事本带 BOM；无 BOM 时按 NUL 出现在奇/偶偏移猜 LE/BE）。"""
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", errors="replace")
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", errors="replace")
    if b"\x00" in data:
        zeros = [i % 2 for i, b in enumerate(data) if b == 0]
        enc = "utf-16-le" if sum(zeros) >= len(zeros) - sum(zeros) else "utf-16-be"
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass   # 猜错或并非 UTF-16，回退常规链
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
