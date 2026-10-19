#!/usr/bin/env python3
"""真实 CLI 的权限 / AskUserQuestion 冒烟测试（默认跳过，会产生模型调用）。

用法:
    CLAUDE_SESSIONS_SMOKE=1 uv run python tests/smoke_permission_live.py
    CLAUDE_SESSIONS_SMOKE=1 SMOKE_ASK=1 uv run python tests/smoke_permission_live.py
    CLAUDE_SESSIONS_SMOKE=1 SMOKE_MODEL=haiku uv run python tests/smoke_permission_live.py

SMOKE_ASK=1 时让模型调用 AskUserQuestion，验证 PreToolUse hook 与网页回答链路；
否则让模型调用 Bash，验证 can_use_tool 审批链路。

前提:
  - claude CLI 已登录，且当前环境允许调用模型。
  - 使用临时目录作为 cwd，脚本结束时删除该会话的转录文件。
  - 如果全局配置自动放行所有工具，权限冒烟可能收不到请求，脚本会说明原因。

退出码:
  0  交互出现、回答后回合正常完成
  2  没有出现预期的交互（环境自动放行、模型未调用工具或上游不可用）
  1  其他失败
"""
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402


PERMISSION_PROMPT = (
    "Use the Bash tool to run exactly this command: echo ok > smoke.txt . "
    "Do not use any other tool. After the command succeeds, reply with exactly DONE."
)
ASK_PROMPT = (
    "Use the AskUserQuestion tool to ask exactly one question. "
    "The question text must be 'Pick one?', with options 'A' and 'B'. "
    "Do not answer it yourself. After the user answers, reply with exactly DONE."
)


def wait_for(cond, timeout, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        value = cond()
        if value:
            return value
        time.sleep(interval)
    return None


def start_server(mon):
    """启动一个本地 HTTP server，使 AskUserQuestion hook 有回调地址。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), monitor.make_handler(mon, 3))
    mon.http_port = server.server_port
    mon.hook_token = secrets.token_hex(16)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def cleanup_session(sid):
    """只删除本次冒烟创建的会话转录与任务目录。"""
    if not sid:
        return
    projects = monitor.CLAUDE_DIR / "projects"
    for path in projects.glob(f"*/*{sid}.jsonl"):
        try:
            if path.stem == sid:
                print(f"[smoke] 删除转录 {path}")
                path.unlink()
        except OSError as exc:
            print(f"[smoke] 删除转录失败: {exc}")
    tasks = monitor.CLAUDE_DIR / "tasks" / sid
    if tasks.is_dir():
        shutil.rmtree(tasks, ignore_errors=True)


def main():
    if os.environ.get("CLAUDE_SESSIONS_SMOKE") != "1":
        print("跳过：设置 CLAUDE_SESSIONS_SMOKE=1 才会执行真实模型冒烟测试")
        return 0

    ask_mode = os.environ.get("SMOKE_ASK") == "1"
    model = os.environ.get("SMOKE_MODEL") or None
    cwd = tempfile.mkdtemp(prefix="claude-sessions-smoke-")
    mon = monitor.Monitor(stale_after=600, permission_timeout=180)
    mon.claude_bin = shutil.which("claude") or "claude"
    mon.claude_args = ["--permission-mode", "default"]
    server = start_server(mon)
    host = None
    sid = None
    try:
        print(f"[smoke] 模式={'ask-user' if ask_mode else 'permission'} "
              f"cwd={cwd} model={model or '默认'} hook_port={mon.http_port}")
        code, resp = mon.host_session(
            cwd, None, ASK_PROMPT if ask_mode else PERMISSION_PROMPT,
            model=model if model else monitor.UNSET,
            approval="full" if ask_mode else "ask")
        print(f"[smoke] host_session -> {code} {resp}")
        if code != 202:
            return 1
        host = mon.hosted.get(resp.get("hosted_id"))
        sid = host.session_id if host else None
        job_id = resp.get("job_id")

        pending = None
        deadline = time.time() + 150
        while time.time() < deadline:
            current = mon.pending_interactions()
            wanted = [i for i in current
                      if (i.get("kind") == "choice") == ask_mode]
            if wanted:
                pending = wanted
                break
            finished = mon.jobs.get(job_id, {})
            if finished.get("status") in ("done", "failed", "cancelled"):
                print("[smoke] 回合在出现预期交互前就结束了。")
                print(f"[smoke] job={finished}")
                print("[smoke] 可能原因：模型没有调用工具，或全局配置自动放行了权限。")
                return 2
            time.sleep(0.2)
        if not pending:
            print("[smoke] 150 秒内没有收到预期交互。")
            print(f"[smoke] job={mon.jobs.get(job_id, {})}")
            if host:
                retry = host.info().get("last_api_retry")
                if retry:
                    print(f"[smoke] 上游正在重试（模型服务不可用？）: {retry}")
            return 2

        inter = pending[0]
        if ask_mode:
            print(f"[smoke] 收到问题: {inter.get('questions')}")
            answers = {}
            for question in inter.get("questions") or []:
                options = question.get("options") or []
                answers[question["question"]] = (options[0]["label"]
                                                 if options else "A")
            code, body = mon.respond_interaction(
                inter["id"], {"behavior": "allow", "answers": answers})
            print(f"[smoke] 回答 {answers} -> {code} state={body.get('state')}")
        else:
            print(f"[smoke] 收到审批: tool={inter['tool_name']} "
                  f"preview={inter['preview']['kind']}")
            code, body = mon.respond_interaction(
                inter["id"], {"behavior": "allow", "scope": "once"})
            print(f"[smoke] 允许 -> {code} state={body.get('state')}")
        if code != 200:
            return 1

        job = wait_for(
            lambda: mon.jobs.get(job_id) if mon.jobs.get(job_id, {}).get("status")
            in ("done", "failed", "cancelled") else None,
            timeout=150)
        if not job:
            print("[smoke] 回合未在 150 秒内结束")
            if host:
                retry = host.info().get("last_api_retry")
                if retry:
                    print(f"[smoke] 上游正在重试（模型服务不可用？）: {retry}")
            return 1
        print(f"[smoke] job={job.get('status')} "
              f"result={monitor.clip(job.get('result') or '', 200)!r}")
        sid = job.get("result_session_id") or sid or (host.session_id if host else None)
        print(f"[smoke] session_id={sid}")
        return 0 if job.get("status") == "done" else 1
    finally:
        if host:
            host.stop()
        server.shutdown()
        server.server_close()
        cleanup_session(sid)
        shutil.rmtree(cwd, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())