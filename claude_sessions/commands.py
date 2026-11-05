"""静态可枚举的 slash 命令目录扫描（新建会话页与未托管会话页的输入提示）。"""
import json
from pathlib import Path

from . import util

_SCAN_TTL = 30.0
_scan_cache = {}   # (claude_dir, cwd) -> (采集时刻, names)


def _scan_commands_dirs(bases):
    """commands 目录下的 *.md，子目录映射为 name/sub。"""
    names = []
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
            if name and not name.startswith("."):
                names.append(name)
    return names


def _scan_skill_dirs(bases):
    """目录型 skill（含 SKILL.md）注册为 /名字；顶层散装 .md 是知识库不是命令。"""
    names = []
    for base in bases:
        try:
            if not base.is_dir():
                continue
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            try:
                if (entry / "SKILL.md").is_file():
                    names.append(entry.name)
            except OSError:
                continue
    return names


def _same_path(a, b):
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return False


def _scan_plugin_commands(claude_dir, cwd):
    """installed_plugins.json 里插件自带的命令与目录型 skill，形式 插件名:名字。

    local scope 只在 projectPath 匹配 cwd 时生效（与 CLI 的项目级安装一致）；
    installed_plugins.json 是 CLI 私有结构，解析失败一律跳过——提示目录宁缺勿错。
    """
    try:
        raw = json.loads((Path(claude_dir) / "plugins" / "installed_plugins.json")
                         .read_text(encoding="utf-8"))
        plugins = raw.get("plugins")
        if not isinstance(plugins, dict):
            return []
    except (OSError, ValueError):
        return []
    names = []
    for key, installs in plugins.items():
        plugin = key.split("@", 1)[0]
        if not plugin or not isinstance(installs, (dict, list)):
            continue
        if isinstance(installs, dict):
            installs = [installs]
        for inst in installs:
            if not isinstance(inst, dict):
                continue
            if inst.get("scope") == "local" and not (
                    isinstance(inst.get("projectPath"), str) and cwd
                    and _same_path(inst["projectPath"], cwd)):
                continue
            install_path = inst.get("installPath")
            if not isinstance(install_path, str):
                continue
            root = Path(install_path).expanduser()
            names += [f"{plugin}:{n}" for n in _scan_skill_dirs([root / "skills"])]
            commands_base = root / "commands"
            try:
                if commands_base.is_dir():
                    for path in sorted(commands_base.glob("*.md")):
                        if path.stem and not path.stem.startswith("."):
                            names.append(f"{plugin}:{path.stem}")
            except OSError:
                continue
    return names


def scan_slash_commands(claude_dir, cwd):
    """收集文件系统上可静态枚举的 slash 命令，返回去 / 前缀的名字列表。

    覆盖用户级 <claude_dir>/commands 与项目级 <cwd>/.claude/commands 下的
    *.md（子目录映射为 name/sub）、两级 skills 目录下的目录型 skill（含
    SKILL.md）、以及插件 commands/*.md（plugin:command）。内置命令不在文件
    系统上，要等首个托管进程 system/init 广播。detail 轮询会高频调用，
    结果缓存 _SCAN_TTL 秒。
    """
    key = (str(claude_dir), str(cwd or ""))
    now = util.now_ts()
    hit = _scan_cache.get(key)
    if hit and now - hit[0] < _SCAN_TTL:
        return list(hit[1])
    user_dir = Path(claude_dir).expanduser()
    proj_dir = Path(cwd).expanduser() / ".claude" if cwd else None
    sources = [_scan_commands_dirs([user_dir / "commands"]
                                   + ([proj_dir / "commands"] if proj_dir else [])),
               _scan_skill_dirs([user_dir / "skills"]
                                + ([proj_dir / "skills"] if proj_dir else [])),
               _scan_plugin_commands(claude_dir, cwd)]
    names, seen = [], set()
    for chunk in sources:
        for name in chunk:
            if name not in seen:
                seen.add(name)
                names.append(name)
    names.sort()
    _scan_cache[key] = (now, names)
    return list(names)
