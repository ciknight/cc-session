"""文件在线预览：分类/解码纯函数与 /file、/view 端点测试。"""
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import monitor
from claude_sessions import config, fileview
from urllib.error import HTTPError


class FileViewPureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_markdown_text_image_binary(self):
        for name in ("a.md", "b.markdown"):
            (self.tmp / name).write_bytes(b"")
            self.assertEqual(fileview.classify_file(self.tmp / name), "markdown")
        for name in ("b.py", "c.json", "c.svg", "notes.txt", "x.log"):
            (self.tmp / name).write_bytes(b"")
            self.assertEqual(fileview.classify_file(self.tmp / name), "text")
        for name in ("d.png", "e.jpg", "f.jpeg", "g.gif", "h.webp", "i.bmp"):
            (self.tmp / name).write_bytes(b"")
            self.assertEqual(fileview.classify_file(self.tmp / name), "image")
        (self.tmp / "z.zip").write_bytes(b"\x00\x01")
        self.assertEqual(fileview.classify_file(self.tmp / "z.zip"), "binary")

    def test_无扩展名按内容嗅探(self):
        (self.tmp / "noext").write_bytes("纯文本内容".encode("utf-8"))
        self.assertEqual(fileview.classify_file(self.tmp / "noext"), "text")
        (self.tmp / "blob").write_bytes(b"\x00\x01\x02")
        self.assertEqual(fileview.classify_file(self.tmp / "blob"), "binary")
        # 常见无扩展名文件名直接按文本处理
        (self.tmp / "Makefile").write_bytes(b"all:\n")
        self.assertEqual(fileview.classify_file(self.tmp / "Makefile"), "text")
        (self.tmp / "LICENSE").write_bytes(b"MIT\n")
        self.assertEqual(fileview.classify_file(self.tmp / "LICENSE"), "text")

    def test_decode_utf8_gbk_replace(self):
        self.assertEqual(fileview.decode_text("中文".encode("utf-8")), "中文")
        self.assertEqual(fileview.decode_text("中文".encode("gb18030")), "中文")
        # 无法识别的字节不抛异常，替换为 U+FFFD
        self.assertIsInstance(fileview.decode_text(b"\xff\xfe\x00abc"), str)


class FileEndpointTests(unittest.TestCase):
    """GET /file：meta 与内容两种模式（复用管理 API 测试的服务器搭法）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_patch = patch.object(config, "CLAUDE_DIR", self.root / ".claude")
        self.config_patch.start()
        self.mon = monitor.Monitor(600)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), monitor.make_handler(self.mon, 3))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.config_patch.stop()
        self.temp.cleanup()

    def get(self, path):
        return urlopen(self.base + path)

    def test_meta_与各类型内容(self):
        (self.root / "doc.md").write_bytes("# 标题\n\n正文".encode("utf-8"))
        (self.root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.root / "run.ps1").write_bytes(b"echo hi")
        meta = json.load(self.get("/file?path=" + urllib.parse.quote(str(self.root / "doc.md"))
                                  + "&meta=1"))
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["kind"], "markdown")
        self.assertEqual(meta["name"], "doc.md")
        self.assertEqual(meta["size"], len("# 标题\n\n正文".encode("utf-8")))
        r = self.get("/file?path=" + urllib.parse.quote(str(self.root / "doc.md")))
        self.assertEqual(r.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(r.read().decode("utf-8"), "# 标题\n\n正文")
        r = self.get("/file?path=" + urllib.parse.quote(str(self.root / "pic.png")))
        self.assertEqual(r.headers["Content-Type"], "image/png")
        self.assertEqual(r.read(), b"\x89PNG\r\n\x1a\n")
        r = self.get("/file?path=" + urllib.parse.quote(str(self.root / "run.ps1")))
        self.assertEqual(r.read().decode("utf-8"), "echo hi")

    def test_加号与中文路径(self):
        target = self.root / "a+b 中文.md"
        target.write_bytes("内容".encode("utf-8"))
        # path 中编码后的 + 不能被 parse_qs 当成空格
        q = urllib.parse.quote(str(target))
        r = self.get("/file?path=" + q)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.read().decode("utf-8"), "内容")

    def test_超大文本截断(self):
        (self.root / "big.log").write_bytes(b"x" * 3_000_000)
        r = self.get("/file?path=" + urllib.parse.quote(str(self.root / "big.log")))
        body = r.read().decode("utf-8")
        self.assertLess(len(body), 3_000_000)
        self.assertIn("[已截断", body)

    def test_错误路径(self):
        # 相对路径与目录 → 400，不存在 → 404
        for path, code in [("relative/x.md", 400), (str(self.root / "nope.md"), 404),
                           (str(self.root), 400)]:
            with self.assertRaises(HTTPError) as ctx:
                self.get("/file?path=" + urllib.parse.quote(path))
            self.assertEqual(ctx.exception.code, code)
        # meta 模式同样校验
        with self.assertRaises(HTTPError) as ctx:
            self.get("/file?path=" + urllib.parse.quote("relative/x.md") + "&meta=1")
        self.assertEqual(ctx.exception.code, 400)
        self.assertFalse(json.load(ctx.exception)["ok"])


if __name__ == "__main__":
    unittest.main()
