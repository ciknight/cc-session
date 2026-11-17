"""常用提示词模板：服务端 JSON 持久化，供发送框一键插入。"""
import json
import math
import re
import secrets
import threading
from pathlib import Path

from session_preferences import _atomic_json_dump

from . import config, util

RECORD_KEYS = {"id", "title", "text", "created_at", "updated_at"}
# 生成 id 为 16 位十六进制；手改文件也只接受 URL 路径安全的字符，保证 DELETE 可用
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
LOAD_ERROR = "提示词模板文件无法读取或格式损坏，修复或移走该文件后会自动重新加载"
SAVE_ERROR = "提示词模板保存失败，请检查目录权限或磁盘空间"
INVALID_TEXT = "模板包含无效的 Unicode 字符"


def _has_surrogate(value):
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _title_from_text(text):
    """标题留空时取正文首个非空行，截到 40 字符。"""
    return text.strip().splitlines()[0].strip()[:40] if text.strip() else ""


def _normalize(title, text):
    if not isinstance(text, str):
        raise ValueError("模板正文必须是字符串")
    text = text.strip()
    if not text:
        raise ValueError("模板正文不能为空")
    if len(text) > config.PROMPT_TEMPLATE_TEXT_CHARS:
        raise ValueError(f"模板正文不能超过 {config.PROMPT_TEMPLATE_TEXT_CHARS} 字符")
    if _has_surrogate(text):
        raise ValueError(INVALID_TEXT)
    if title is None:
        title = ""
    if not isinstance(title, str):
        raise ValueError("模板标题必须是字符串")
    title = title.strip() or _title_from_text(text)
    if _has_surrogate(title):
        raise ValueError(INVALID_TEXT)
    if len(title) > config.PROMPT_TEMPLATE_TITLE_CHARS:
        raise ValueError(f"模板标题不能超过 {config.PROMPT_TEMPLATE_TITLE_CHARS} 字符")
    return title, text


def _valid_timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _record_valid(record, seen_ids):
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        return False
    template_id, title, text = record["id"], record["title"], record["text"]
    if (not isinstance(template_id, str) or not ID_RE.fullmatch(template_id)
            or template_id in seen_ids):
        return False
    if not isinstance(title, str) or not title.strip():
        return False
    if not isinstance(text, str) or not text.strip():
        return False
    if _has_surrogate(title) or _has_surrogate(text):
        return False
    if (len(title) > config.PROMPT_TEMPLATE_TITLE_CHARS
            or len(text) > config.PROMPT_TEMPLATE_TEXT_CHARS):
        return False
    if not _valid_timestamp(record["created_at"]) or not _valid_timestamp(record["updated_at"]):
        return False
    seen_ids.add(template_id)
    return True


class PromptTemplates:
    """`~/.claude/claude-sessions/prompt-templates.json` 的线程安全封装。

    整个文件校验通过才加载；任一记录损坏都置 error 并拒绝读写，
    避免把截断/手改坏的数据再写回去。写操作原子落盘，成功后才发布内存状态。
    每次操作前比对文件 mtime/size：运行期间手改文件会被重新加载；文件损坏时
    修复后也能自动恢复，不需要重启服务。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.templates = []
        self.error = None
        self.file_stamp = None
        self._refresh_locked()

    def _stat_stamp(self):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return False
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh_locked(self):
        stamp = self._stat_stamp()
        if stamp is False:
            self.error = LOAD_ERROR
            return
        if stamp is None:
            # 文件被移走/删除时以空列表为准，下一次写入会重新创建文件。
            self.templates = []
            self.error = None
            self.file_stamp = None
            return
        if stamp == self.file_stamp and self.error is None:
            return
        self.file_stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or not isinstance(data.get("templates"), list)):
                raise ValueError("格式或版本不支持")
            if len(data["templates"]) > config.PROMPT_TEMPLATE_LIMIT:
                raise ValueError("模板数量超过上限")
            loaded, seen_ids = [], set()
            for record in data["templates"]:
                if not _record_valid(record, seen_ids):
                    raise ValueError("模板记录无效")
                loaded.append({key: record[key] for key in
                               ("id", "title", "text", "created_at", "updated_at")})
            self.templates = loaded
            self.error = None
        except (OSError, ValueError, RecursionError):
            self.error = LOAD_ERROR

    def list(self):
        with self.lock:
            self._refresh_locked()
            self._require_available()
            return [dict(record) for record in self.templates]

    def create(self, title, text):
        with self.lock:
            self._refresh_locked()
            self._require_available()
            if len(self.templates) >= config.PROMPT_TEMPLATE_LIMIT:
                raise ValueError(f"模板数量已达上限 {config.PROMPT_TEMPLATE_LIMIT} 条")
            title, text = _normalize(title, text)
            now = util.now_ts()
            record = {
                "id": secrets.token_hex(8),
                "title": title,
                "text": text,
                "created_at": now,
                "updated_at": now,
            }
            updated = [record] + self.templates
            self._save(updated)
            self.templates = updated
            return dict(record)

    def update(self, template_id, title, text):
        with self.lock:
            self._refresh_locked()
            self._require_available()
            title, text = _normalize(title, text)
            for index, record in enumerate(self.templates):
                if record["id"] == template_id:
                    updated_record = dict(record, title=title, text=text,
                                          updated_at=util.now_ts())
                    updated = list(self.templates)
                    updated[index] = updated_record
                    self._save(updated)
                    self.templates = updated
                    return dict(updated_record)
            raise KeyError(template_id)

    def delete(self, template_id):
        with self.lock:
            self._refresh_locked()
            self._require_available()
            for index, record in enumerate(self.templates):
                if record["id"] == template_id:
                    updated = self.templates[:index] + self.templates[index + 1:]
                    self._save(updated)
                    self.templates = updated
                    return True
            return False

    def _require_available(self):
        if self.error:
            raise RuntimeError(self.error)

    def _save(self, templates):
        try:
            _atomic_json_dump(self.path, {"version": 1, "templates": templates},
                              "prompt-templates-")
        except OSError as exc:
            raise RuntimeError(SAVE_ERROR) from exc
        except UnicodeError as exc:
            raise RuntimeError(SAVE_ERROR) from exc
        stamp = self._stat_stamp()
        self.file_stamp = stamp if stamp is not False else None