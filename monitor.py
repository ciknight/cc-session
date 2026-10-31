#!/usr/bin/env python3
"""monitor.py — 监听 Claude Code 会话的任务内容和状态（入口与兼容层）。

实现按职责拆在 claude_sessions 包中：
  core.py       Monitor：会话注册表、轮询扫描、远程消息/队列、审批协调、事件分发
  hosted.py     Hosted：常驻 claude 进程（stream-json 双向）
  session.py    Session：单个转录的累计状态与对外投影
  transcript.py 转录行解析（对话消息提取）
  http_api.py   HTTP/SSE 层
  interactions.py / system.py / commands.py / util.py / config.py / hook.py / app.py

本文件 re-export 公共符号，保持 `import monitor` 的旧用法可用；
完整接口与用法说明见 claude_sessions/app.py 的模块文档。

用法:
  uv run monitor.py                    # 启动服务（默认 http://0.0.0.0:8765）
  uv run monitor.py --port 9000 --stale 600
  uv run monitor.py --webhook http://localhost:3000/hook
"""
import sys

from claude_sessions.config import (CLAUDE_DIR, TEMPLATE_DIR, STATIC_DIR,
                                    MSG_TAIL_BYTES, MSG_CHUNK_BYTES,
                                    SCAN_CACHE_SAVE_SECONDS, SCAN_CACHE_SAVE_SUBAGENTS_SECONDS,
                                    SCAN_TAIL_SAMPLE_BYTES, REMOTE_QUEUE_LIMIT,
                                    MSG_CACHE_SESSION_LIMIT, WEBHOOK_QUEUE_LIMIT)
from claude_sessions.util import now_ts, clip, project_root, path_is_within, strip_ansi
from claude_sessions.system import SystemInfoUnavailable, system_snapshot, power_command, spawn_power
from claude_sessions.commands import scan_slash_commands
from claude_sessions.session import Session
from claude_sessions.transcript import classify_user_line, parse_tool_result, parse_msg_line
from claude_sessions.interactions import Interaction, permission_preview
from claude_sessions.hosted import Hosted
from claude_sessions.core import Monitor
from claude_sessions.http_api import make_handler
from claude_sessions.hook import hook_client
from claude_sessions.app import main
from session_preferences import UNSET

if __name__ == "__main__":
    sys.exit(main())
