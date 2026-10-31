"""命令行入口：监听 Claude Code 会话的任务内容和状态。

数据来源:
  ~/.claude/projects/<项目>/<sessionId>.jsonl   会话转录，逐行追加
  ~/.claude/tasks/<sessionId>/*.json            任务清单（如果存在，作为补充）

接口 (默认 http://127.0.0.1:8765):
  GET /                       网页看板：左侧项目列表，右侧会话列表/对话视图，自动刷新可开关
  GET /sessions               所有会话概览（?full=1 时含任务、子代理、最近事件等完整信息）
  GET /sessions/<id>          按 id 取单个会话完整信息
  GET /sessions/<id>/messages 按需解析对话；?limit=100&before=N 向前分页，?from=N 增量追加
  GET /interactions           待审批的权限请求（?session=<id> 可过滤）
  POST /interactions/<id>/respond 审批 {"behavior":"allow|deny","scope":"once|always"}
  POST /sessions/<id>/interrupt   中断托管会话当前回合并清空排队消息
  POST /sessions/<id>/approval    设置审批模式 {"approval":"ask|auto|full"}
  POST /sessions/<id>/effort      设置托管思考档位 {"effort":"low|medium|high|default"}
  GET /static/<file>          前端静态资源（marked / DOMPurify）
  GET /events                 SSE 实时事件流（?session=<id> 可过滤；首帧为 snapshot，
                              流式文本为 transient turn_delta）
  POST /hook/PreToolUse       AskUserQuestion 的 hook 回调（仅回环 + X-Monitor-Token）
  GET /healthz                健康检查
  GET /system                宿主机 CPU/内存利用率快照（psutil 采样，缺依赖时 503）
  POST /system/power         电源操作 {"action": "restart|sleep"}，仅前端 confirm 防误触
  POST /system/sleep-timer   定时睡眠 {"minutes": 0-720}（0 取消，服务端计时，重启不恢复）

hook 子命令（由托管进程的内联 --settings 调用，不面向用户）:
  python monitor.py hook --port <port> --token <token>

远程发消息:
  POST /sessions/<id>/message  给会话发消息 {"text": "...", "force": false}，返回 job_id。
                               内部执行 `claude -p --resume <id>`（工作目录取会话的 cwd，
                               环境变量原样继承，所以自建 API / ANTHROPIC_BASE_URL 同样适用）
  GET /jobs/<job_id>           查询发消息任务的状态和回复 (queued / running / done / failed)
  POST /new                    {"cwd": "<目录>", "text": "首条消息"} 在该目录跑一轮全新会话
                               （不托管，每条消息重启一次 claude）；job 完成后
                               result_session_id 即新会话 id

托管会话（常驻 claude 进程，后续消息直接写 stdin，不再每次重启）:
  POST /hosted                 {"resume_id": "<已有会话id>" 或 "cwd": "<新建会话的目录>",
                               "text": "第一条消息", "force": false,
                               "approval": "ask|auto|full"}
                               之后给该会话 POST /sessions/<id>/message 会自动走常驻进程，
                               模型忙时消息排队，一轮结束再发下一条
  GET /hosted                  托管进程列表（状态、是否忙、排队数、审批模式、轮数）
  DELETE /hosted/<hid>         正常退出（做完当前一轮）；加 ?kill=1 立即终止
  托管进程带 --permission-prompt-tool stdio：ask 模式在网页审批，auto 交给 CLI（默认模式），
  full 自动放行；超时（--permission-timeout，默认 300 秒）自动拒绝。
  ExitPlanMode 显示计划卡片；AskUserQuestion 通过 PreToolUse hook 在网页回答。
  注意: 要托管已有会话，先在终端里把它退出，避免两个进程同时写一个会话。

接口无鉴权，发消息/托管可远程执行 claude，请只在可信网络内暴露。

每个会话可展示的信息（均解析自转录文件）:
  标题(ai-title)、项目目录(cwd)、git 分支、模型、Claude 版本、
  首/末事件时间(时长)、用户/助手消息数、token 用量(input/output/cache)、
  工具调用统计、子代理(Agent 工具)列表、任务清单(TaskCreate/TaskUpdate 或 tasks 目录)

会话状态:
  working  有用户输入/工具调用/子代理活动，或仍有未完成的工具调用
  idle     收到 system/turn_duration（回退：从未出现该标记时才用 stop_reason=end_turn）
  stale    超过 --stale 秒没有任何内容事件
  双时钟: last_input_ts 只由真实用户输入推进；last_event_ts 取最后一条带时间戳的事件。

用法:
  python monitor.py                      # 监听默认目录
  python monitor.py --port 9000 --stale 600
  python monitor.py --scan-subagents     # 启动时连带扫描子代理转录（默认懒加载）
  python monitor.py --webhook http://localhost:3000/hook   # 事件同时 POST 出去
"""
import argparse
import secrets
import shlex
import shutil
import sys
import threading
from http.server import ThreadingHTTPServer

from . import config
from .core import Monitor
from .http_api import make_handler
from .hook import hook_client


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        return hook_client(sys.argv[2:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔(秒)")
    ap.add_argument("--refresh", type=float, default=3.0, help="网页自动刷新间隔(秒)")
    ap.add_argument("--stale", type=int, default=300, help="多少秒无更新视为 stale")
    ap.add_argument("--webhook", help="把每个事件 POST 到该 URL")
    ap.add_argument("--claude-bin", default="claude", help="claude 命令路径")
    ap.add_argument("--claude-args", default="",
                    help='发消息时附加给 claude 的参数，例如 "--permission-mode acceptEdits --model sonnet"')
    ap.add_argument("--msg-timeout", type=int, default=600, help="单条远程消息最长执行秒数")
    ap.add_argument("--permission-timeout", type=int, default=300,
                    help="托管会话审批等待秒数，0 表示无限等待")
    ap.add_argument("--scan-subagents", action="store_true",
                    help="启动时扫描子代理转录（默认懒加载，仅在显式请求时读取）")
    args = ap.parse_args()
    if args.permission_timeout < 0:
        ap.error("--permission-timeout 不能为负数（0 表示无限等待）")

    mon = Monitor(args.stale, args.webhook, args.permission_timeout)
    # Windows 上 npm 安装的 claude 是 claude.cmd，这里先解析出完整路径
    mon.claude_bin = shutil.which(args.claude_bin) or args.claude_bin
    mon.claude_args = shlex.split(args.claude_args)
    mon.msg_timeout = args.msg_timeout
    mon.scan_subagents = args.scan_subagents
    threading.Thread(target=mon.run, args=(args.interval,), daemon=True).start()
    print(f"[monitor] 监听 {config.CLAUDE_DIR / 'projects'}，HTTP http://{args.host}:{args.port}")
    if not args.scan_subagents:
        print("[monitor] 子代理转录懒加载（需要时用 --scan-subagents 或 include_subagents=1）")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[monitor] 警告: 接口无鉴权且可远程执行 claude，请务必只暴露在可信网络/隧道之后")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(mon, args.refresh))
    mon.http_port = server.server_port
    mon.hook_token = secrets.token_hex(16)
    try:
        server.serve_forever()
    finally:
        # 正常退出（含 Ctrl+C）时先停轮询并落盘扫描状态，再收尾托管进程。
        mon.shutdown_event.set()
        mon.maybe_save_cache(force=True)
        # 关 stdin 让托管进程做完当前一轮再退；强杀场景由管道 EOF 兜底。
        server.server_close()
        for h in list(mon.hosted.values()):
            try:
                h.stop()
                print(f"[hosted] {h.hid} 已随服务退出 (code {h.returncode})", flush=True)
            except Exception as e:
                print(f"[hosted] {h.hid} 退出失败: {e}", flush=True)
    return 0
