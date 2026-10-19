#!/usr/bin/env python3
"""真实 CLI 的权限回合冒烟测试（默认跳过，会产生一次模型调用）。

用法:
    CLAUDE_SESSIONS_SMOKE=1 uv run python tests/smoke_permission_live.py
    CLAUDE_SESSIONS_SMOKE=1 SMOKE_MODEL=haiku uv run python tests/smoke_permission_live.py

前提:
  - claude CLI 已登录，且当前环境允许调用模型。
  - 使用临时目录作为 cwd，脚本结束时删除该会话的转录文件。
  - 如果你的全局配置自动放行所有工具，可能收不到审批请求，脚本会失败并说明原因。

退出码:
  0  权限请求出现、允许后回合正常完成
  2  没有出现审批请求（环境自动放行或工具未触发）
  1  其他失败
"""
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import monitor  # noqa: E402


PROMPT = (
    "Use the Bash tool to run exactly this command: echo ok > smoke.txt . "
    "Do not use any other tool. After the command succeeds, reply with exactly DONE."
)


def wait_for(cond, timeout, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        value = cond()
        if value:
            return value
        time.sleep(interval)
    return None


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

    model = os.environ.get("SMOKE_MODEL") or None
    cwd = tempfile.mkdtemp(prefix="claude-sessions-smoke-")
    mon = monitor.Monitor(stale_after=600, permission_timeout=180)
    mon.claude_bin = shutil.which("claude") or "claude"
    # 强制 ask 模式，确保工具调用会走 can_use_tool
    mon.claude_args = ["--permission-mode", "default"]
    host = None
    sid = None
    try:
        print(f"[smoke] 启动真实托管进程 cwd={cwd} model={model or '默认'}")
        code, resp = mon.host_session(cwd, None, PROMPT, model=model if model else monitor.UNSET,
                                      approval="ask")
        print(f"[smoke] host_session -> {code} {resp}")
        if code != 202:
            return 1
        host = mon.hosted.get(resp.get("hosted_id"))
        # 即使回合失败/超时也用它清理本次会话的转录
        sid = host.session_id if host else None
        job_id = resp.get("job_id")

        pending = None
        deadline = time.time() + 120
        while time.time() < deadline:
            current = mon.pending_interactions()
            if current:
                pending = current
                break
            finished = mon.jobs.get(job_id, {})
            if finished.get("status") in ("done", "failed", "cancelled"):
                print("[smoke] 回合在出现审批请求前就结束了。")
                print(f"[smoke] job={finished}")
                print("[smoke] 可能原因：全局配置自动放行了 Bash，或模型没有调用工具。")
                return 2
            time.sleep(0.2)
        if not pending:
            print("[smoke] 120 秒内没有收到审批请求。")
            print(f"[smoke] job={mon.jobs.get(job_id, {})}")
            if host:
                retry = host.info().get("last_api_retry")
                if retry:
                    print(f"[smoke] 上游正在重试（模型服务不可用？）: {retry}")
            return 2

        inter = pending[0]
        print(f"[smoke] 收到审批: tool={inter['tool_name']} preview={inter['preview']['kind']}")
        code, body = mon.respond_interaction(inter["id"], {"behavior": "allow", "scope": "once"})
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
        print(f"[smoke] job={job.get('status')} result={monitor.clip(job.get('result') or '', 200)!r}")
        sid = job.get("result_session_id") or sid or (host.session_id if host else None)
        print(f"[smoke] session_id={sid}")
        return 0 if job.get("status") == "done" else 1
    finally:
        if host:
            host.stop()
        cleanup_session(sid)
        shutil.rmtree(cwd, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())