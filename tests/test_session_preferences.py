import json
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


if __name__ == "__main__":
    unittest.main()
