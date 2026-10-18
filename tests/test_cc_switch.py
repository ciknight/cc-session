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
