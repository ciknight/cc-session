"""自定义 slash 命令目录扫描（新建会话页的输入提示）。"""
from pathlib import Path


def scan_slash_commands(claude_dir, cwd):
    """扫描文件系统上的自定义 slash 命令，返回去 / 前缀的名字列表。

    只覆盖用户级 <claude_dir>/commands 与项目级 <cwd>/.claude/commands 下的 *.md
    （子目录映射为 name/sub）；内置命令要等首个托管进程 system/init 广播才有，
    新建会话页在进程启动前用这份目录做输入提示。
    """
    names, seen = [], set()
    bases = []
    try:
        bases.append(Path(claude_dir).expanduser() / "commands")
        bases.append(Path(cwd).expanduser() / ".claude" / "commands")
    except (TypeError, RuntimeError, OSError):
        return []
    for base in bases:
        try:
            if not base.is_dir():
                continue
            files = sorted(base.rglob("*.md"))
        except OSError:
            continue
        for path in files:
            rel = path.relative_to(base)
            if any(part.startswith(".") for part in rel.parts[:-1]):
                continue
            name = rel.as_posix()[:-len(".md")]
            if name and not name.startswith(".") and name not in seen:
                seen.add(name)
                names.append(name)
    return sorted(names)
