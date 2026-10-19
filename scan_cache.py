"""转录扫描状态缓存：重启后只解析新增字节。"""
import json
import os
import tempfile
import threading
from pathlib import Path

CACHE_VERSION = 1


class ScanCache:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.error = None
        self.files = {}

    def load(self):
        """读取缓存；单条损坏只丢弃该条，整体损坏则从空缓存开始。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
                raise ValueError("格式或版本不支持")
            files = data.get("files")
            if not isinstance(files, dict):
                raise ValueError("files 字段无效")
            self.files = {
                key: value for key, value in files.items()
                if isinstance(key, str) and isinstance(value, dict)
                and isinstance(value.get("offset"), int) and value["offset"] >= 0
                and isinstance(value.get("tail_sha256"), str)
                and isinstance(value.get("buf"), str)
                and isinstance(value.get("session"), dict)
            }
        except FileNotFoundError:
            self.files = {}
        except (OSError, ValueError, json.JSONDecodeError):
            self.error = "扫描缓存无法读取或格式损坏，已忽略"
            self.files = {}
        return self.files

    def save(self, files):
        payload = {"version": CACHE_VERSION, "files": files}
        temp = None
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temp = tempfile.mkstemp(
                    prefix="scan-cache-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
                self.files = files
            finally:
                if temp and os.path.exists(temp):
                    os.unlink(temp)