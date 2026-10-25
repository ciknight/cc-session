import json
from concurrent.futures import ThreadPoolExecutor
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_preferences as prefs


class PreferenceTests(unittest.TestCase):
    def test_model_validation(self):
        for model in (None, "opus", "custom/model-v2", "供应商模型"):
            self.assertEqual(prefs.validate_model(model), model)
        for model in ("", " opus", "opus ", "--dangerous", "x\ny", "a\x7fb", 1, [], {}, "x" * 257):
            with self.subTest(model=model), self.assertRaises(ValueError):
                prefs.validate_model(model)

    def test_arguments_keep_default_and_override_only_model(self):
        args = ["--model", "sonnet", "--verbose", "--model=haiku"]
        self.assertEqual(prefs.model_args(args, None), args)
        self.assertEqual(prefs.model_args(args, "custom"), ["--verbose", "--model=custom"])
        self.assertEqual(args[1], "sonnet")
        self.assertEqual(prefs.default_model(args), "haiku")

    def test_persist_and_clear_by_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "preferences.json"
            store = prefs.ModelPreferences(path)
            store.set("a", "opus")
            store.set("b", "sonnet")
            loaded = prefs.ModelPreferences(path)
            self.assertEqual(loaded.get("a"), "opus")
            loaded.set("a", None)
            self.assertIsNone(loaded.get("a"))
            self.assertEqual(loaded.get("b"), "sonnet")
            self.assertEqual(json.loads(path.read_text("utf-8"))["version"], 1)

    def test_write_failure_does_not_change_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = prefs.ModelPreferences(Path(tmp) / "preferences.json")
            store.set("a", "opus")
            with patch("session_preferences.os.replace", side_effect=OSError("fail")):
                with self.assertRaises(ValueError):
                    store.set("a", "sonnet")
            self.assertEqual(store.get("a"), "opus")

    def test_corrupt_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "preferences.json"
            path.write_text("broken", encoding="utf-8")
            store = prefs.ModelPreferences(path)
            self.assertIsNone(store.get("a"))
            self.assertTrue(store.error)
            with self.assertRaises(ValueError):
                store.set("a", "opus")
            self.assertEqual(path.read_text("utf-8"), "broken")

    def test_new_session_binding_does_not_replace_later_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = prefs.ModelPreferences(Path(tmp) / "preferences.json")
            store.set("a", "sonnet")
            store.bind("a", "opus")
            self.assertEqual(store.get("a"), "sonnet")
            store.bind("b", "opus")
            self.assertEqual(store.get("b"), "opus")


class ApprovalPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "approvals.json"
        self.addCleanup(self.tmp.cleanup)

    def test_default_auto_and_persist(self):
        store = prefs.ApprovalPreferences(self.path)
        self.assertEqual(store.get("sid-1"), "auto")
        store.set("sid-1", "ask")
        self.assertEqual(prefs.ApprovalPreferences(self.path).get("sid-1"), "ask")

    def test_invalid_mode_rejected(self):
        store = prefs.ApprovalPreferences(self.path)
        with self.assertRaises(ValueError):
            store.set("sid-1", "bypass")

    def test_corrupt_file_does_not_overwrite(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{broken", encoding="utf-8")
        store = prefs.ApprovalPreferences(self.path)
        self.assertTrue(store.error)
        with self.assertRaises(ValueError):
            store.set("sid-1", "auto")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")


class MetadataValidationTests(unittest.TestCase):
    def test_alias_normalization_and_length_boundary(self):
        for value, expected in ((None, None), ("", None), ("   ", None),
                                ("  会话名称  ", "会话名称"),
                                (" " + "名" * 120 + " ", "名" * 120)):
            with self.subTest(value=value):
                self.assertEqual(prefs.validate_metadata_patch({"alias": value}),
                                 {"alias": expected})
        for value in ("名" * 121, 1, False, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prefs.validate_metadata_patch({"alias": value})

    def test_controls_rejected_before_trimming(self):
        for value in ("\nname", "name\t", "\r", "\t", "a\x00b", "a\x7fb",
                      "a\x85b", "a​b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prefs.validate_metadata_patch({"alias": value})

    def test_boolean_fields_require_actual_bool(self):
        for field in ("pinned", "archived"):
            for value in (True, False):
                self.assertEqual(prefs.validate_metadata_patch({field: value}), {field: value})
            for value in (0, 1, None, "true", [], {}):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    prefs.validate_metadata_patch({field: value})

    def test_empty_unknown_and_non_dict_rejected(self):
        for value in ({}, None, [], "alias", {"metadata_revision": 1}, {"revision": 1},
                      {"alias": "ok", "unknown": True}, {1: True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prefs.validate_metadata_patch(value)

    def test_normalization_returns_independent_partial_copy(self):
        original = {"alias": "  名称  ", "pinned": True}
        result = prefs.validate_metadata_patch(original)
        self.assertEqual(result, {"alias": "名称", "pinned": True})
        self.assertEqual(original, {"alias": "  名称  ", "pinned": True})
        result["pinned"] = False
        self.assertTrue(original["pinned"])


class SessionMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "private" / "session-metadata.json"
        self.addCleanup(self.tmp.cleanup)

    def defaults(self, revision=0):
        return {"alias": None, "pinned": False, "archived": False,
                "metadata_revision": revision}

    def test_default_does_not_create_file(self):
        store = prefs.SessionMetadata(self.path)
        self.assertEqual(store.get("a"), self.defaults())
        self.assertFalse(self.path.exists())

    def test_partial_update_clear_and_session_isolation(self):
        store = prefs.SessionMetadata(self.path)
        self.assertEqual(store.update("a", {"alias": "  名称  "}),
                         dict(self.defaults(1), alias="名称"))
        store.update("a", {"pinned": True})
        self.assertEqual(store.update("a", {"archived": True}),
                         {"alias": "名称", "pinned": True, "archived": True,
                          "metadata_revision": 3})
        self.assertEqual(store.get("b"), self.defaults())
        store.update("b", {"alias": "另一会话"})
        store.update("a", {"alias": "   ", "pinned": False, "archived": False})
        self.assertEqual(store.get("a"), self.defaults(4))
        self.assertEqual(store.get("b"), dict(self.defaults(1), alias="另一会话"))
        data = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(data, {"version": 1, "sessions": {
            "a": {"alias": None, "pinned": False, "archived": False, "revision": 4},
            "b": {"alias": "另一会话", "pinned": False, "archived": False, "revision": 1}}})

    def test_pin_and_archive_remain_independent(self):
        store = prefs.SessionMetadata(self.path)
        store.update("a", {"pinned": True})
        store.update("a", {"archived": True})
        self.assertEqual(store.update("a", {"archived": False}),
                         dict(self.defaults(3), pinned=True))
        store.update("a", {"archived": True})
        self.assertEqual(store.update("a", {"pinned": False}),
                         dict(self.defaults(5), archived=True))

    def test_revision_persists_across_restart_and_default_reset(self):
        store = prefs.SessionMetadata(self.path)
        store.update("a", {"alias": "名称"})
        store.update("a", {"alias": None})
        loaded = prefs.SessionMetadata(self.path)
        self.assertEqual(loaded.get("a"), self.defaults(2))
        self.assertEqual(loaded.update("a", {"pinned": True}),
                         dict(self.defaults(3), pinned=True))
        self.assertEqual(prefs.SessionMetadata(self.path).get("a"),
                         dict(self.defaults(3), pinned=True))

    def test_get_and_update_return_copies(self):
        store = prefs.SessionMetadata(self.path)
        initial = store.get("a")
        initial["alias"] = "外部修改"
        self.assertEqual(store.get("a"), self.defaults())
        result = store.update("a", {"alias": "名称"})
        result["alias"] = "外部修改"
        result["metadata_revision"] = 99
        snapshot = store.get("a")
        snapshot["pinned"] = True
        self.assertEqual(store.get("a"), dict(self.defaults(1), alias="名称"))

    def test_invalid_update_leaves_disk_and_memory_unchanged(self):
        store = prefs.SessionMetadata(self.path)
        store.update("a", {"alias": "名称"})
        before = self.path.read_bytes()
        for value in ({}, {"pinned": 1}, {"alias": "bad\n"}, {"revision": 20}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store.update("a", value)
        self.assertEqual(store.get("a"), dict(self.defaults(1), alias="名称"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_threaded_partial_updates_merge_without_lost_fields_or_revisions(self):
        store = prefs.SessionMetadata(self.path)
        barrier = threading.Barrier(3)

        def update_field(field, value):
            barrier.wait(timeout=10)
            for _ in range(10):
                store.update("a", {field: value})

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(update_field, field, value) for field, value in
                       (("alias", "名称"), ("pinned", True), ("archived", True))]
            for future in futures:
                future.result(timeout=30)
        expected = {"alias": "名称", "pinned": True, "archived": True,
                    "metadata_revision": 30}
        self.assertEqual(store.get("a"), expected)
        self.assertEqual(prefs.SessionMetadata(self.path).get("a"), expected)

    def test_corrupt_versions_structures_and_records_block_all_writes(self):
        valid = {"alias": None, "pinned": False, "archived": False, "revision": 1}
        bad_records = [None, [], {}, dict(valid, alias=123), dict(valid, alias="bad\n"),
                       dict(valid, pinned=1), dict(valid, archived=None),
                       dict(valid, revision=True), dict(valid, revision=-1),
                       dict(valid, revision=1.5), dict(valid, extra="unknown")]
        documents = ["broken", "[]", '{"version":2,"sessions":{}}',
                     '{"version":true,"sessions":{}}', '{"version":1}',
                     '{"version":1,"sessions":[]}']
        documents += [json.dumps({"version": 1, "sessions": {"good": valid, "bad": record}})
                      for record in bad_records]
        self.path.parent.mkdir(parents=True)
        for document in documents:
            with self.subTest(document=document):
                self.path.write_text(document, encoding="utf-8")
                store = prefs.SessionMetadata(self.path)
                self.assertEqual(store.get("good"), self.defaults())
                with self.assertRaises(ValueError):
                    store.update("new", {"pinned": True})
                self.assertEqual(self.path.read_text("utf-8"), document)

    def test_invalid_utf8_blocks_writes(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff")
        store = prefs.SessionMetadata(self.path)
        self.assertEqual(store.get("a"), self.defaults())
        with self.assertRaises(ValueError):
            store.update("a", {"pinned": True})
        self.assertEqual(self.path.read_bytes(), b"\xff")

    def test_replace_failure_preserves_memory_disk_and_revision_and_cleans_temp(self):
        store = prefs.SessionMetadata(self.path)
        store.update("a", {"alias": "名称"})
        before = self.path.read_bytes()
        with patch("session_preferences.os.replace", side_effect=OSError("fail")):
            with self.assertRaises(ValueError):
                store.update("a", {"pinned": True})
        self.assertEqual(store.get("a"), dict(self.defaults(1), alias="名称"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(store.update("a", {"archived": True})["metadata_revision"], 2)

    def test_fsync_failure_does_not_commit_memory_or_replace_file(self):
        store = prefs.SessionMetadata(self.path)
        with patch("session_preferences.os.fsync", side_effect=OSError("fail")):
            with self.assertRaises(ValueError):
                store.update("a", {"pinned": True})
        self.assertEqual(store.get("a"), self.defaults())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_atomic_write_uses_same_directory_and_commits_memory_after_replace(self):
        store = prefs.SessionMetadata(self.path)
        original_replace = prefs.os.replace

        def replace(source, target):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(target), self.path)
            self.assertEqual(store.sessions, {})
            data = json.loads(Path(source).read_text("utf-8"))
            self.assertEqual(data["sessions"]["a"]["revision"], 1)
            original_replace(source, target)

        with patch("session_preferences.os.replace", side_effect=replace), \
                patch("session_preferences.os.fsync", wraps=prefs.os.fsync) as fsync:
            store.update("a", {"pinned": True})
        fsync.assert_called_once()
        self.assertEqual(store.get("a"), dict(self.defaults(1), pinned=True))


class EffortPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "efforts.json"
        self.addCleanup(self.tmp.cleanup)

    def test_validate_effort(self):
        self.assertEqual(prefs.validate_effort("low"), "low")
        self.assertEqual(prefs.validate_effort("high"), "high")
        self.assertIsNone(prefs.validate_effort("default"))
        for level in ("", "default ", "high ", "ultra", None, 1, [], {}):
            with self.subTest(level=level), self.assertRaises(ValueError):
                prefs.validate_effort(level)

    def test_arguments_override_only_effort(self):
        args = ["--effort", "low", "--verbose", "--effort=high"]
        self.assertEqual(prefs.effort_args(args, None), args)
        self.assertEqual(prefs.effort_args(args, "medium"), ["--verbose", "--effort=medium"])
        self.assertEqual(args[1], "low")

    def test_persist_and_clear_by_session(self):
        store = prefs.EffortPreferences(self.path)
        self.assertIsNone(store.get("a"))
        store.set("a", "high")
        store.set("b", "low")
        self.assertEqual(prefs.EffortPreferences(self.path).get("a"), "high")
        store.set("a", None)
        self.assertIsNone(store.get("a"))
        self.assertEqual(store.get("b"), "low")
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["version"], 1)

    def test_invalid_level_rejected_and_corrupt_file(self):
        store = prefs.EffortPreferences(self.path)
        with self.assertRaises(ValueError):
            store.set("a", "ultra")
        self.assertIsNone(store.get("a"))
        self.path.write_text("broken", encoding="utf-8")
        broken = prefs.EffortPreferences(self.path)
        self.assertTrue(broken.error)
        with self.assertRaises(ValueError):
            broken.set("a", "low")
        self.assertEqual(self.path.read_text("utf-8"), "broken")

    def test_bind_does_not_replace_later_choice(self):
        store = prefs.EffortPreferences(self.path)
        store.set("a", "low")
        store.bind("a", "high")
        self.assertEqual(store.get("a"), "low")
        store.bind("b", "medium")
        self.assertEqual(store.get("b"), "medium")


if __name__ == "__main__":
    unittest.main()
