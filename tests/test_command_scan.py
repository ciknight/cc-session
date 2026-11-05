import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_sessions import commands, util


class SlashCommandScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.claude_dir = self.root / ".claude"
        self.project = self.root / "proj"
        self.project.mkdir()
        commands._scan_cache.clear()

    def tearDown(self):
        self.temp.cleanup()

    def test_user_and_project_commands_and_skills(self):
        (self.claude_dir / "commands" / "sub").mkdir(parents=True)
        (self.claude_dir / "commands" / "sub" / "dep.md").write_text("# /dep", encoding="utf-8")
        (self.claude_dir / "skills" / "deploy").mkdir(parents=True)
        (self.claude_dir / "skills" / "deploy" / "SKILL.md").write_text("deploy", encoding="utf-8")
        (self.claude_dir / "skills" / "notes.md").write_text("知识库，不是命令", encoding="utf-8")
        (self.project / ".claude" / "skills" / "local-skill").mkdir(parents=True)
        (self.project / ".claude" / "skills" / "local-skill" / "SKILL.md").write_text("x", encoding="utf-8")
        names = commands.scan_slash_commands(self.claude_dir, str(self.project))
        self.assertEqual(names, ["deploy", "local-skill", "sub/dep"])

    def test_empty_cwd_skips_project_sources(self):
        (self.claude_dir / "commands").mkdir(parents=True)
        (self.claude_dir / "commands" / "global.md").write_text("# /global", encoding="utf-8")
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, ""), ["global"])
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, None), ["global"])

    def test_plugin_commands_honor_local_scope(self):
        cache = self.claude_dir / "plugins" / "cache"
        shared = cache / "commit" / "1.0"
        (shared / "commands").mkdir(parents=True)
        (shared / "commands" / "commit.md").write_text("# /commit", encoding="utf-8")
        (shared / "skills" / "design").mkdir(parents=True)
        (shared / "skills" / "design" / "SKILL.md").write_text("design", encoding="utf-8")
        local = cache / "lsp" / "2.0"
        (local / "commands").mkdir(parents=True)
        (local / "commands" / "jump.md").write_text("# /jump", encoding="utf-8")
        other = self.root / "other-proj"
        other.mkdir()
        (self.claude_dir / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
            # 单对象形态（user 级）
            "commit@market": {"scope": "user", "installPath": str(shared)},
            # 数组形态；local 只对 projectPath 匹配的项目生效
            "lsp@market": [{"scope": "local", "projectPath": str(self.project),
                            "installPath": str(local)}],
            "broken@market": "not-an-install",
        }}), encoding="utf-8")
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, str(self.project)),
                         ["commit:commit", "commit:design", "lsp:jump"])
        # local 插件对其他项目不出现；不同 cwd 是不同缓存键
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, str(other)),
                         ["commit:commit", "commit:design"])

    def test_broken_or_missing_plugin_manifest_is_tolerated(self):
        (self.claude_dir / "plugins").mkdir(parents=True)
        # 无 installed_plugins.json
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, str(self.project)), [])
        manifest = self.claude_dir / "plugins" / "installed_plugins.json"
        manifest.write_text("{broken json", encoding="utf-8")
        commands._scan_cache.clear()
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, str(self.project)), [])
        manifest.write_text(json.dumps({"plugins": []}), encoding="utf-8")
        commands._scan_cache.clear()
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, str(self.project)), [])

    def test_scan_result_is_cached_with_ttl(self):
        (self.claude_dir / "commands").mkdir(parents=True)
        (self.claude_dir / "commands" / "a.md").write_text("# /a", encoding="utf-8")
        base = util.now_ts()
        with patch.object(util, "now_ts", side_effect=[base, base + 1, base + 100]):
            first = commands.scan_slash_commands(self.claude_dir, str(self.project))
            (self.claude_dir / "commands" / "b.md").write_text("# /b", encoding="utf-8")
            second = commands.scan_slash_commands(self.claude_dir, str(self.project))   # TTL 内命中缓存
            third = commands.scan_slash_commands(self.claude_dir, str(self.project))    # 过期重扫
        self.assertEqual(first, ["a"])
        self.assertEqual(second, ["a"])
        self.assertEqual(third, ["a", "b"])
        # 缓存返回拷贝，调用方修改不污染缓存
        third.append("hacked")
        self.assertEqual(commands.scan_slash_commands(self.claude_dir, str(self.project)), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
