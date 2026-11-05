import tempfile
import unittest
from pathlib import Path

from claude_sessions import util


class ResolveClaudeBinTests(unittest.TestCase):
    def test_is_windows_batch_path(self):
        self.assertTrue(util.is_windows_batch_path(r"C:\npm\claude.cmd"))
        self.assertTrue(util.is_windows_batch_path("claude.CMD"))
        self.assertTrue(util.is_windows_batch_path(r"C:\a.bat\claude.exe"))
        self.assertFalse(util.is_windows_batch_path(r"C:\bin\claude.exe"))
        self.assertFalse(util.is_windows_batch_path(
            "node_modules/@anthropic-ai/claude-code/bin/claude.exe"))
        self.assertFalse(util.is_windows_batch_path(""))

    def test_non_windows_keeps_shim(self):
        resolved = util.resolve_claude_bin(
            "claude", which=lambda name: "/usr/local/bin/claude",
            platform_name="Linux")
        self.assertEqual(resolved, "/usr/local/bin/claude")

    def test_windows_native_exe_detection(self):
        self.assertTrue(util.is_windows_native_exe(r"C:\bin\claude.EXE"))
        self.assertTrue(util.is_windows_native_exe("claude.com"))
        self.assertFalse(util.is_windows_native_exe("/f/nodejs/claude"))
        self.assertFalse(util.is_windows_native_exe("claude.cmd"))

    def test_windows_prefers_native_for_extensionless_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shim = Path(tmp) / "claude"
            shim.write_text("#!/bin/sh\n")
            exe = (Path(tmp) / "node_modules" / "@anthropic-ai" / "claude-code"
                   / "bin" / "claude.exe")
            exe.parent.mkdir(parents=True)
            exe.write_text("")

            def which(name):
                return str(shim) if name == "claude" else None

            resolved = util.resolve_claude_bin("claude", which=which,
                                               platform_name="Windows", home=home)
        self.assertEqual(resolved, str(exe))

    def test_explicit_batch_path_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            wrapper = Path(tmp) / "tools" / "custom-wrapper.cmd"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("")
            other_exe = Path(tmp) / "other" / "claude.exe"
            other_exe.parent.mkdir()
            other_exe.write_text("")

            def which(name):
                if name == "claude.exe":
                    return str(other_exe)
                return str(wrapper) if str(name) == str(wrapper) else None

            resolved = util.resolve_claude_bin(str(wrapper), which=which,
                                               platform_name="Windows", home=home)
        self.assertEqual(resolved, str(wrapper))

    def test_custom_command_name_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shim = Path(tmp) / "my-claude.cmd"
            shim.write_text("")
            other_exe = Path(tmp) / "other" / "claude.exe"
            other_exe.parent.mkdir()
            other_exe.write_text("")

            def which(name):
                if name == "claude.exe":
                    return str(other_exe)
                return str(shim) if name == "my-claude" else None

            resolved = util.resolve_claude_bin("my-claude", which=which,
                                               platform_name="Windows", home=home)
        self.assertEqual(resolved, str(shim))

    def test_windows_prefers_sibling_native_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "claude.cmd"
            shim.write_text("")
            exe = (Path(tmp) / "node_modules" / "@anthropic-ai" / "claude-code"
                   / "bin" / "claude.exe")
            exe.parent.mkdir(parents=True)
            exe.write_text("")

            def which(name):
                return str(shim) if name == "claude" else None

            resolved = util.resolve_claude_bin("claude", which=which,
                                               platform_name="Windows",
                                               home=Path(tmp) / "home")
        self.assertEqual(resolved, str(exe))

    def test_windows_prefers_path_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "claude.cmd"
            shim.write_text("")
            exe = Path(tmp) / "native" / "claude.exe"
            exe.parent.mkdir()
            exe.write_text("")

            def which(name):
                return {"claude": str(shim), "claude.exe": str(exe)}.get(name)

            resolved = util.resolve_claude_bin("claude", which=which,
                                               platform_name="Windows",
                                               home=Path(tmp) / "home")
        self.assertEqual(resolved, str(exe))

    def test_windows_keeps_batch_when_no_native_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shim = Path(tmp) / "claude.cmd"
            shim.write_text("")

            def which(name):
                return str(shim) if name == "claude" else None

            resolved = util.resolve_claude_bin("claude", which=which,
                                               platform_name="Windows", home=home)
        self.assertEqual(resolved, str(shim))


class ChildEnvTests(unittest.TestCase):
    def test_drops_parent_session_vars_keeps_user_config(self):
        base = {
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION_ID": "parent",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CODE_MESSAGING_SOCKET": "pipe",
            "ANTHROPIC_API_KEY": "key",
            "HTTP_PROXY": "http://proxy",
            "PATH": "/bin",
        }
        env = util.child_env(cwd=r"F:\code\project", base=base)
        for key in util.CHILD_ENV_DROP:
            self.assertNotIn(key, env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "key")
        self.assertEqual(env["HTTP_PROXY"], "http://proxy")
        self.assertEqual(env["PWD"], r"F:\code\project")
        # 不修改传入的 base
        self.assertIn("CLAUDECODE", base)


class ResultErrorTextTests(unittest.TestCase):
    def test_combines_structured_fields(self):
        data = {
            "result": "API Error: 529 overloaded",
            "errors": ["overloaded", "overloaded"],
            "api_error_status": 529,
            "terminal_reason": "api_error",
            "subtype": "error_during_execution",
        }
        self.assertEqual(
            util.result_error_text(data),
            "API Error: 529 overloaded | overloaded | HTTP 529 | api_error")

    def test_falls_back_to_subtype_then_text(self):
        self.assertEqual(util.result_error_text({"subtype": "error_max_turns"}),
                         "error_max_turns")
        self.assertEqual(util.result_error_text({}, "stderr detail"),
                         "stderr detail")
        self.assertEqual(util.result_error_text(None, "stderr detail"),
                         "stderr detail")

    def test_accepts_string_errors(self):
        self.assertEqual(util.result_error_text({"errors": "boom"}),
                         "boom")

    def test_success_subtype_is_not_an_error_reason(self):
        self.assertEqual(util.result_error_text({"subtype": "success"}), "error")
        self.assertEqual(util.result_error_text({}, ""), "error")


class ActiveRateLimitTests(unittest.TestCase):
    def test_expired_record_is_hidden(self):
        self.assertIsNone(util.active_rate_limit(
            {"status": "rejected", "resets_at": 100}, now=101))
        self.assertEqual(
            util.active_rate_limit({"status": "allowed", "resets_at": 200},
                                   now=100)["status"],
            "allowed")

    def test_record_without_reset_or_invalid(self):
        self.assertEqual(util.active_rate_limit({"status": "rejected"}, now=100),
                         {"status": "rejected"})
        self.assertIsNone(util.active_rate_limit(None, now=100))
        self.assertIsNone(util.active_rate_limit("bad", now=100))


if __name__ == "__main__":
    unittest.main()