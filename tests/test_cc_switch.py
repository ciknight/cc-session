import io
import json
import os
import subprocess
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

    def write_local(self, data, project=None):
        target = (project or self.project) / ".claude" / "settings.local.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")
        return target

    def test_status_returns_only_sanitized_current_local_model_options(self):
        self.write_local({"_ccSwitchProfile": "safe", "env": {
            "OPUS_MODEL": "local-opus", "SONNET_MODEL": "local-sonnet",
            "HAIKU_MODEL": "local-haiku", "FABLE_MODEL": "local-fable",
            "ANTHROPIC_MODEL": "astra", "ANTHROPIC_AUTH_TOKEN": "secret"},
            "endpoint": "https://endpoint-sentinel.invalid"})
        profile = self.home / ".cc-switch" / "profiles" / "safe.json"
        profile.write_text(json.dumps({"env": {"OPUS_MODEL": "unused-profile-model"}}), encoding="utf-8")
        original_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            self.assertNotEqual(path.parent, self.app.profile_dir, "模型选项不能读取 profile 正文")
            return original_read_text(path, *args, **kwargs)

        with (patch("cc_switch.shutil.which", return_value="cc-switch"),
              patch.object(Path, "read_text", autospec=True, side_effect=read_text)):
            result = self.app.status(str(self.project))
        self.assertEqual(result["model_options"], [
            {"name": "Opus: local-opus", "model": "local-opus"},
            {"name": "Sonnet: local-sonnet", "model": "local-sonnet"},
            {"name": "Haiku: local-haiku", "model": "local-haiku"},
            {"name": "Fable: local-fable", "model": "local-fable"},
        ])
        self.assertEqual(result["current_profile"], "safe")
        serialized = json.dumps(result)
        for value in ("secret", "ANTHROPIC_MODEL", '"env"', "endpoint-sentinel", "unused-profile-model"):
            self.assertNotIn(value, serialized)

    def test_status_skips_invalid_local_models_without_generic_fallback(self):
        self.write_local({"model": "k3", "env": {
            "ANTHROPIC_MODEL": "astra", "OPUS_MODEL": 7, "SONNET_MODEL": "valid-sonnet",
            "HAIKU_MODEL": "", "FABLE_MODEL": None}})
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            result = self.app.status(str(self.project))
        self.assertEqual(result["model_options"], [
            {"name": "Sonnet: valid-sonnet", "model": "valid-sonnet"}])

    def test_status_supports_prefixed_local_fields_with_explicit_field_priority(self):
        self.write_local({"env": {
            "OPUS_MODEL": "explicit-opus", "ANTHROPIC_DEFAULT_OPUS_MODEL": "ignored-opus",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "prefixed-sonnet",
            "HAIKU_MODEL": 7, "ANTHROPIC_DEFAULT_HAIKU_MODEL": "ignored-invalid-fallback",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "prefixed-fable",
        }})
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            result = self.app.status(str(self.project))
        self.assertEqual(result["model_options"], [
            {"name": "Opus: explicit-opus", "model": "explicit-opus"},
            {"name": "Sonnet: prefixed-sonnet", "model": "prefixed-sonnet"},
            {"name": "Fable: prefixed-fable", "model": "prefixed-fable"},
        ])

    def test_absent_or_invalid_local_never_falls_back_to_profiles_or_global_settings(self):
        profile = self.home / ".cc-switch" / "profiles" / "safe.json"
        profile.write_text('{"env":{"OPUS_MODEL":"profile-model"}}', encoding="utf-8")
        self.config.mkdir()
        (self.config / "settings.json").write_text(
            '{"env":{"OPUS_MODEL":"global-model"}}', encoding="utf-8")
        target = self.project / ".claude" / "settings.local.json"
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            self.assertEqual(self.app.status(str(self.project))["model_options"], [])
            target.parent.mkdir()
            for contents in ("not json", "[]", "{}", '{"env":[]}', '{"env":{}}'):
                with self.subTest(contents=contents):
                    target.write_text(contents, encoding="utf-8")
                    self.assertEqual(self.app.status(str(self.project))["model_options"], [])

    def test_local_read_error_does_not_expose_exception(self):
        target = self.write_local({"env": {"SONNET_MODEL": "sonnet"}})
        original_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == target:
                raise OSError("endpoint-read-error-sentinel")
            return original_read_text(path, *args, **kwargs)

        with (patch("cc_switch.shutil.which", return_value="cc-switch"),
              patch.object(Path, "read_text", autospec=True, side_effect=read_text)):
            result = self.app.status(str(self.project))
        self.assertEqual(result["model_options"], [])
        self.assertNotIn("endpoint-read-error-sentinel", json.dumps(result))

    def test_local_model_options_do_not_require_cc_switch_or_profile_directory(self):
        self.write_local({"env": {"FABLE_MODEL": "local-model"}})
        with (patch("cc_switch.shutil.which", return_value=None),
              patch.object(self.app, "profile_dir", self.root / "missing-profiles")):
            result = self.app.status(str(self.project))
        self.assertEqual(result["model_options"], [{"name": "Fable: local-model", "model": "local-model"}])
        self.assertFalse(result["installed"])
        self.assertFalse(result["available"])
        self.assertEqual(result["profiles"], [])

    def test_local_options_are_isolated_by_cwd_and_reread_after_config_changes(self):
        other = self.root / "other-project"
        other.mkdir()
        self.write_local({"env": {"OPUS_MODEL": "first"}})
        self.write_local({"env": {"OPUS_MODEL": "other"}}, other)
        with patch("cc_switch.shutil.which", return_value="cc-switch"):
            self.assertEqual(self.app.status(str(self.project))["model_options"][0]["model"], "first")
            self.assertEqual(self.app.status(str(other))["model_options"][0]["model"], "other")
            self.write_local({"env": {"OPUS_MODEL": "changed"}})
            self.assertEqual(self.app.status(str(self.project))["model_options"][0]["model"], "changed")

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

    @unittest.skipUnless(os.name == "nt", "仅 Windows 使用 CMD shim")
    def test_cmd_shim_runs_with_spaces_and_preserves_arguments_and_cwd(self):
        binary = self.root / "installed cli" / "cc-switch.cmd"
        binary.parent.mkdir()
        binary.write_text('@echo off\necho %1\necho %2\ncd\n', encoding="utf-8")
        proc = subprocess.run(
            self.app._command_argv(str(binary), "safe"), cwd=str(self.project),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["local", "safe", str(self.project)])

    def test_cli_failure_logs_safe_reason_and_exit_code_without_raw_output(self):
        proc = subprocess.CompletedProcess([], 7, stdout="bare-secret\n[http] forged",
                                           stderr="'secret-command' is not recognized as an internal or external command")
        output = io.StringIO()
        with (patch("cc_switch.shutil.which", return_value="cc-switch"),
              patch("cc_switch.subprocess.run", return_value=proc),
              patch("sys.stdout", output)):
            with self.assertRaises(SwitchError) as raised:
                self.app.apply(str(self.project), "safe", "absent", confirm=True)
        log = output.getvalue()
        self.assertIn("[cc-switch]", log)
        self.assertIn("退出码=7", log)
        self.assertIn("命令无法识别", log)
        for secret in ("bare-secret", "secret-command", "forged"):
            self.assertNotIn(secret, log + raised.exception.message)
        self.assertEqual(len(log.splitlines()), 1)
        self.assertIn("7", raised.exception.message)

    def test_cli_timeout_and_launch_error_log_safe_diagnostics(self):
        for failure, code, expected in (
            (subprocess.TimeoutExpired("secret-command", 30, output=b"secret-output",
                                       stderr=b"secret-stderr"), 504, "执行超时"),
            (OSError(13, "secret-error"), 503, "errno=13"),
        ):
            with self.subTest(failure=type(failure).__name__):
                output = io.StringIO()
                with (patch("cc_switch.shutil.which", return_value="cc-switch"),
                      patch("cc_switch.subprocess.run", side_effect=failure),
                      patch("sys.stdout", output)):
                    with self.assertRaises(SwitchError) as raised:
                        self.app.apply(str(self.project), "safe", "absent", confirm=True)
                self.assertEqual(raised.exception.code, code)
                self.assertIn(expected, output.getvalue())
                self.assertNotIn("secret", output.getvalue() + raised.exception.message)

    def test_verification_failure_logs_safe_stage(self):
        target = self.project / ".claude" / "settings.local.json"
        target.parent.mkdir()
        for contents, expected in (("secret-invalid-json", "目标配置无法读取或解析"),
                                   ('{"_ccSwitchProfile":"secret-wrong"}', "目标 profile 不匹配")):
            with self.subTest(contents=contents):
                target.write_text(contents, encoding="utf-8")
                output = io.StringIO()
                with (patch("cc_switch.shutil.which", return_value="cc-switch"),
                      patch("cc_switch.subprocess.run", return_value=subprocess.CompletedProcess([], 0)),
                      patch("sys.stdout", output)):
                    revision = self.app.status(str(self.project))["revision"]
                    with self.assertRaises(SwitchError):
                        self.app.apply(str(self.project), "safe", revision, confirm=True)
                self.assertIn(expected, output.getvalue())
                self.assertNotIn("secret", output.getvalue())

    def test_logging_failure_does_not_replace_switch_error(self):
        with (patch("cc_switch.shutil.which", return_value="cc-switch"),
              patch("cc_switch.subprocess.run", return_value=subprocess.CompletedProcess([], 2)),
              patch("builtins.print", side_effect=OSError("log unavailable"))):
            with self.assertRaises(SwitchError) as raised:
                self.app.apply(str(self.project), "safe", "absent", confirm=True)
        self.assertEqual(raised.exception.code, 500)

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
