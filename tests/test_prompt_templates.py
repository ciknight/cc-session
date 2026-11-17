import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import monitor
from claude_sessions import config, prompts


class PromptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "prompt-templates.json"
        self.limit_patch = patch.object(config, "PROMPT_TEMPLATE_LIMIT", 3)
        self.limit_patch.start()
        self.now_patch = patch.object(prompts.util, "now_ts", return_value=1000.0)
        self.now_patch.start()
        self.store = prompts.PromptTemplates(self.path)

    def tearDown(self):
        self.now_patch.stop()
        self.limit_patch.stop()
        self.temp.cleanup()

    def test_create_update_delete_and_reload(self):
        first = self.store.create("  修 bug  ", "  按仓库风格修复  ")
        self.assertEqual(first["title"], "修 bug")
        self.assertEqual(first["text"], "按仓库风格修复")
        self.assertEqual(first["created_at"], 1000.0)
        second = self.store.create("", "写测试\n覆盖边界")
        self.assertEqual(second["title"], "写测试")

        updated = self.store.update(first["id"], "", "新的正文")
        self.assertEqual(updated["title"], "新的正文")
        self.assertEqual(updated["created_at"], first["created_at"])
        self.assertTrue(self.store.delete(second["id"]))
        self.assertFalse(self.store.delete(second["id"]))
        with self.assertRaises(KeyError):
            self.store.update("missing", "x", "y")

        reloaded = prompts.PromptTemplates(self.path)
        self.assertEqual([t["id"] for t in reloaded.list()], [first["id"]])
        self.assertEqual(reloaded.list()[0]["text"], "新的正文")

    def test_validation_and_limits(self):
        for title, text in (("", "   "), ("x", ""), (123, "y")):
            with self.assertRaises(ValueError):
                self.store.create(title, text)
        with self.assertRaises(ValueError):
            self.store.create("x" * 81, "正文")
        with self.assertRaises(ValueError):
            self.store.create("x", "y" * 20001)

        derived = self.store.create(None, "标题行\n正文")
        self.assertEqual(derived["title"], "标题行")
        self.store.create("a", "1")
        self.store.create("b", "2")
        with self.assertRaises(ValueError):
            self.store.create("c", "3")

    def test_corrupt_file_blocks_mutations_and_keeps_other_features_alive(self):
        self.path.write_text("{not json", encoding="utf-8")
        broken = prompts.PromptTemplates(self.path)
        with self.assertRaises(RuntimeError):
            broken.list()
        with self.assertRaises(RuntimeError):
            broken.create("a", "b")

        duplicate = {
            "version": 1,
            "templates": [
                {"id": "same", "title": "a", "text": "1", "created_at": 1, "updated_at": 1},
                {"id": "same", "title": "b", "text": "2", "created_at": 2, "updated_at": 2},
            ],
        }
        self.path.write_text(json.dumps(duplicate), encoding="utf-8")
        self.assertIsNotNone(prompts.PromptTemplates(self.path).error)

    def test_save_failure_does_not_publish_in_memory_state(self):
        self.store.create("a", "1")
        with patch.object(prompts, "_atomic_json_dump", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                self.store.create("b", "2")
        self.assertEqual([t["title"] for t in self.store.list()], ["a"])

    def test_concurrent_creates_keep_all_records(self):
        errors = []

        def worker(index):
            try:
                self.store.create(f"t{index}", f"body{index}")
            except Exception as exc:   # pragma: no cover - 失败时由断言报告
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        records = self.store.list()
        self.assertEqual(len(records), 3)
        self.assertEqual(len({record["id"] for record in records}), 3)

    def test_external_file_changes_are_reloaded(self):
        self.store.create("a", "1")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["templates"].append({
            "id": "external1", "title": "external", "text": "x",
            "created_at": 2, "updated_at": 2})
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual([t["title"] for t in self.store.list()], ["a", "external"])

        self.store.create("b", "2")
        reloaded = prompts.PromptTemplates(self.path)
        self.assertEqual({t["title"] for t in reloaded.list()}, {"a", "external", "b"})

    def test_corrupt_file_recovers_after_fix(self):
        self.path.write_text("{bad", encoding="utf-8")
        broken = prompts.PromptTemplates(self.path)
        with self.assertRaises(RuntimeError):
            broken.list()
        good = {"version": 1, "templates": [{
            "id": "ok1", "title": "ok", "text": "x",
            "created_at": 1, "updated_at": 1}]}
        self.path.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual([t["title"] for t in broken.list()], ["ok"])

    def test_accepts_well_formed_emoji(self):
        record = self.store.create("", "标题 😀\n正文")
        self.assertEqual(record["title"], "标题 😀")
        reloaded = prompts.PromptTemplates(self.path)
        self.assertEqual(reloaded.list()[0]["text"], "标题 😀\n正文")

    def test_rejects_invalid_ids_surrogates_and_nonfinite_timestamps(self):
        with self.assertRaises(ValueError) as error:
            self.store.create("ok", "bad " + chr(0xD83D))
        self.assertIn("无效", str(error.exception))

        base = {"id": "ok1", "title": "t", "text": "x",
                "created_at": 1, "updated_at": 1}
        for record in ({**base, "id": "a/b"},
                       {**base, "created_at": float("nan")},
                       {**base, "created_at": 10 ** 1000}):
            self.path.write_text(json.dumps({"version": 1, "templates": [record]}),
                                 encoding="utf-8")
            self.assertIsNotNone(prompts.PromptTemplates(self.path).error)


class PromptTemplateAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_patch = patch.object(config, "CLAUDE_DIR", self.root / ".claude")
        self.config_patch.start()
        self.mon = monitor.Monitor(600)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), monitor.make_handler(self.mon, 3))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.config_patch.stop()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        body = None if data is None else json.dumps(data).encode()
        req = Request(self.base + path, data=body,
                      headers=headers or {}, method="GET" if data is None else "POST")
        return urlopen(req)

    def test_crud_and_validation(self):
        headers = {"Content-Type": "application/json"}
        created = json.load(self.request("/prompt-templates",
                                         {"title": "", "text": "修 bug\n细节"}, headers))
        tid = created["template"]["id"]
        self.assertEqual(created["template"]["title"], "修 bug")
        self.assertEqual(json.load(self.request("/prompt-templates"))["templates"][0]["id"], tid)

        updated = json.load(self.request("/prompt-templates",
                                         {"id": tid, "title": "新标题", "text": "新正文"}, headers))
        self.assertEqual(updated["template"]["title"], "新标题")

        for payload in ({"text": ""}, {"text": 1}, {"id": tid, "text": ""},
                        {"title": "x" * 81, "text": "ok"},
                        {"title": "ok", "text": "y" * 20001}):
            with self.assertRaises(HTTPError) as error:
                self.request("/prompt-templates", payload, headers)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/prompt-templates", {"id": "missing", "text": "x"}, headers)
        self.assertEqual(error.exception.code, 404)

        with self.assertRaises(HTTPError) as error:
            self.request("/prompt-templates", {"text": "bad " + chr(0xD83D)}, headers)
        self.assertEqual(error.exception.code, 400)
        self.assertIn("无效", json.load(error.exception)["error"])

        req = Request(self.base + "/prompt-templates/" + tid, method="DELETE")
        self.assertEqual(json.load(urlopen(req)), {"ok": True})
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + "/prompt-templates/" + tid, method="DELETE"))
        self.assertEqual(error.exception.code, 404)

    def test_cross_site_post_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/prompt-templates", {"text": "x"},
                         {"Content-Type": "application/json",
                          "Origin": "http://evil.invalid"})
        self.assertEqual(error.exception.code, 403)

    def test_corrupt_file_only_breaks_template_endpoints(self):
        path = self.root / ".claude" / "claude-sessions" / "prompt-templates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{bad", encoding="utf-8")
        self.mon.prompts = prompts.PromptTemplates(path)
        rebuilt = monitor.Monitor(600)   # 构造本身不能因为坏模板文件失败
        self.assertIsNotNone(rebuilt.prompts.error)

        with self.assertRaises(HTTPError) as error:
            self.request("/prompt-templates")
        self.assertEqual(error.exception.code, 500)
        self.assertEqual(urlopen(self.base + "/sessions").status, 200)

        headers = {"Content-Type": "application/json"}
        with self.assertRaises(HTTPError) as error:
            self.request("/prompt-templates", {"text": "x"}, headers)
        self.assertEqual(error.exception.code, 500)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + "/prompt-templates/abc", method="DELETE"))
        self.assertEqual(error.exception.code, 500)