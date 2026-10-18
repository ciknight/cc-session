import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cc_switch import CCSwitch, SwitchError


class CCSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.config = self.home / ".claude"
        self.project = self.root / "project"
        self.project.mkdir()
        (self.home / ".cc-switch" / "profiles").mkdir(parents=True)
        (self.home / ".cc-switch" / "profiles" / "safe.json").write_text("{}", encoding="utf-8")
        self.app = CCSwitch(self.config, home=self.home, command="cc-switch")

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_returns_sanitized_profile_model_options(self):
        profiles = self.home / ".cc-switch" / "profiles"
        (profiles / "alias.json").write_text(
            json.dumps({"name": "工作模型", "model": "sonnet",
                        "env": {"ANTHROPIC_MODEL": "vendor-model"}, "api_key": "secret",
                        "endpoint": "https://endpoint-sentinel.invalid"}),
            encoding="utf-8",
        )
        (profiles / "fallback.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            result = self.app.status(str(self.project))

        self.assertEqual(result["model_options"], [
            {"name": "工作模型", "model": "vendor-model"},
            {"name": "fallback", "model": "opus"},
        ])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("ANTHROPIC_MODEL", serialized)
        self.assertNotIn('"env"', serialized)
        self.assertNotIn("endpoint-sentinel", serialized)
        self.assertNotIn('"endpoint"', serialized)

    def test_status_skips_invalid_profile_models_without_invalid_env_fallback(self):
        profiles = self.home / ".cc-switch" / "profiles"
        (profiles / "invalid.json").write_text(json.dumps({"name": "无模型"}), encoding="utf-8")
        (profiles / "bad-env.json").write_text(
            json.dumps({"model": "should-not-fallback", "env": {"ANTHROPIC_MODEL": 7}}),
            encoding="utf-8",
        )
        (profiles / "empty-env.json").write_text(
            json.dumps({"model": "haiku", "env": {}}), encoding="utf-8"
        )
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            result = self.app.status(str(self.project))

        self.assertEqual(result["model_options"], [{"name": "empty-env", "model": "haiku"}])

    def test_status_skips_malformed_and_non_object_profiles_and_falls_back_for_names(self):
        profiles = self.home / ".cc-switch" / "profiles"
        (profiles / "malformed.json").write_text("{not json", encoding="utf-8")
        (profiles / "non-object.json").write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        (profiles / "empty-name.json").write_text(
            json.dumps({"name": "", "model": "sonnet"}), encoding="utf-8"
        )
        (profiles / "non-string-name.json").write_text(
            json.dumps({"name": 12, "model": "opus"}), encoding="utf-8"
        )
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            result = self.app.status(str(self.project))

        self.assertEqual(result["model_options"], [
            {"name": "empty-name", "model": "sonnet"},
            {"name": "non-string-name", "model": "opus"},
        ])

    def test_status_ignores_profile_read_error_without_exposing_exception(self):
        profiles = self.home / ".cc-switch" / "profiles"
        failing_profile = profiles / "read-error.json"
        failing_profile.write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
        original_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == failing_profile:
                raise OSError("endpoint-read-error-sentinel")
            return original_read_text(path, *args, **kwargs)

        with (patch("cc_switch.shutil.which", return_value="cc-switch"),
              patch.object(Path, "read_text", autospec=True, side_effect=read_text)):
            result = self.app.status(str(self.project))

        self.assertEqual(result["model_options"], [])
        self.assertNotIn("endpoint-read-error-sentinel", json.dumps(result))

    def test_model_options_remain_available_when_cc_switch_is_not_installed(self):
        profiles = self.home / ".cc-switch" / "profiles"
        (profiles / "model.json").write_text(
            json.dumps({"name": "本地模型", "model": "vendor-model-id"}), encoding="utf-8"
        )
        with patch("cc_switch.shutil.which", return_value=None):
            result = self.app.status(str(self.project))

        self.assertEqual(result["model_options"], [{"name": "本地模型", "model": "vendor-model-id"}])
        self.assertFalse(result["installed"])
        self.assertFalse(result["available"])
        self.assertEqual(result["profiles"], [])

    def test_status_only_lists_profile_names_and_revision(self):
        target = self.project / ".claude" / "settings.local.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"_ccSwitchProfile": "safe", "env": {"TOKEN": "secret"}}), encoding="utf-8")
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            result = self.app.status(str(self.project))
        self.assertEqual(result["profiles"], ["safe"])
        self.assertEqual(result["current_profile"], "safe")
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(result["revision"])

    def test_apply_requires_confirmation_and_backups_before_cli(self):
        target = self.project / ".claude" / "settings.local.json"
        target.parent.mkdir()
        target.write_bytes(b'{"old":true}')
        with patch("cc_switch.shutil.which", return_value="cc-switch"), patch("cc_switch.subprocess.run") as run:
            status = self.app.status(str(self.project))
            with self.assertRaises(SwitchError) as raised:
                self.app.apply(str(self.project), "safe", status["revision"])
            self.assertEqual(raised.exception.code, 400)
            run.assert_not_called()

    def test_apply_runs_installed_cli_and_verifies_marker(self):
        target = self.project / ".claude" / "settings.local.json"
        target.parent.mkdir()
        target.write_bytes(b'{"old":true}')
        with patch("cc_switch.shutil.which", return_value="cc-switch"), patch("cc_switch.subprocess.run") as run:
            def call(cmd, **kwargs):
                target.write_text('{"_ccSwitchProfile":"safe"}', encoding="utf-8")
                self.assertEqual(kwargs["cwd"], str(self.project.resolve()))
                self.assertEqual(cmd[-2:], ["local", "safe"])
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run.side_effect = call
            status = self.app.status(str(self.project))
            result = self.app.apply(str(self.project), "safe", status["revision"], confirm=True)
        self.assertTrue(result["backed_up"])
        self.assertEqual(Path(result["backup_path"]).read_bytes(), b'{"old":true}')
        self.assertEqual(result["current_profile"], "safe")

    def test_uninstalled_never_falls_back_or_writes(self):
        target = self.project / ".claude" / "settings.local.json"
        with patch("cc_switch.shutil.which", return_value=None):
            result = self.app.status(str(self.project))
            self.assertFalse(result["installed"])
            with self.assertRaises(SwitchError) as raised:
                self.app.apply(str(self.project), "safe", result["revision"], confirm=True)
        self.assertEqual(raised.exception.code, 503)
        self.assertFalse(target.exists())

    def test_changed_revision_and_invalid_profile_are_rejected(self):
        with patch("cc_switch.shutil.which", return_value="cc-switch"), patch("cc_switch.subprocess.run") as run:
            status = self.app.status(str(self.project))
            (self.project / ".claude").mkdir()
            (self.project / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(SwitchError) as raised:
                self.app.apply(str(self.project), "safe", status["revision"], confirm=True)
            self.assertEqual(raised.exception.code, 409)
            with self.assertRaises(SwitchError):
                self.app.apply(str(self.project), "../safe", self.app.status(str(self.project))["revision"], confirm=True)
            run.assert_not_called()

    def test_non_default_claude_dir_disables_operation(self):
        custom = CCSwitch(self.root / "other-config", home=self.home, command="cc-switch")
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            result = custom.status(str(self.project))
        self.assertFalse(result["available"])
        self.assertIn("CLAUDE_CONFIG_DIR", result["reason"])

    def test_cli_failure_does_not_expose_output_and_keeps_backup(self):
        target = self.project / ".claude" / "settings.local.json"
        target.parent.mkdir()
        target.write_bytes(b'{"apiKey":"secret"}')
        with patch("cc_switch.shutil.which", return_value="cc-switch"), patch("cc_switch.subprocess.run") as run:
            run.return_value = type("Result", (), {"returncode": 1, "stdout": "secret", "stderr": "secret"})()
            status = self.app.status(str(self.project))
            with self.assertRaises(SwitchError) as raised:
                self.app.apply(str(self.project), "safe", status["revision"], confirm=True)
        self.assertNotIn("secret", raised.exception.message)
        backups = list((self.config / "claude-sessions" / "backups").rglob("*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'{"apiKey":"secret"}')

    def test_backup_failure_prevents_cli_execution(self):
        target = self.project / ".claude" / "settings.local.json"
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")
        with (patch("cc_switch.shutil.which", return_value="cc-switch"),
              patch("cc_switch.tempfile.mkstemp", side_effect=OSError("disk full")),
              patch("cc_switch.subprocess.run") as run):
            status = self.app.status(str(self.project))
            with self.assertRaises(SwitchError) as raised:
                self.app.apply(str(self.project), "safe", status["revision"], confirm=True)
        self.assertEqual(raised.exception.code, 500)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
