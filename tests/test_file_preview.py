"""文件在线预览：分类/解码纯函数与 /file、/view 端点测试。"""
import tempfile
import unittest
from pathlib import Path

from claude_sessions import fileview


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


if __name__ == "__main__":
    unittest.main()
