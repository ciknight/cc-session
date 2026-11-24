"""测试专用 stream-json 进程，不连接模型服务。"""
import json
import sys
import threading
import time

model = None
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--model" and i + 1 < len(args):
        model = args[i + 1]
    elif arg.startswith("--model="):
        model = arg.split("=", 1)[1]

_write_lock = threading.Lock()
pending_permission_uuid = None
pending_interrupt_uuid = None
cancelled = set()
fast_opt_in = False        # apply_flag_settings 注入的 fastMode opt-in
fast_on = False            # /fast 的会话级开关
turn_busy = threading.Event()   # 有回合进行中：/fast 等它结束（模拟 CLI 命令队列）


def emit(obj):
    with _write_lock:
        print(json.dumps(obj), flush=True)


def complete(uuid, text):
    result = {"type": "result", "session_id": "fake-session", "result": text}
    if fast_on:
        # 真实 CLI 在 result 帧附带当前 fast 状态（thin client 靠它同步）
        result["fast_mode_state"] = "on"
    emit(result)
    emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "completed"})


def slow_turn(uuid, text):
    turn_busy.set()
    try:
        time.sleep(5 if text == "interrupt-me" else 0.15)
        if uuid not in cancelled:
            complete(uuid, str(model) + ":" + text)
    finally:
        turn_busy.clear()


for line in sys.stdin:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    typ = obj.get("type")
    if typ == "control_request":
        request = obj.get("request") or {}
        subtype = request.get("subtype")
        if subtype == "set_model":
            value = request.get("model")
            if value == "timeout":
                continue
            if value == "reject":
                emit({"type": "control_response", "response": {
                    "subtype": "error", "request_id": obj["request_id"],
                    "error": "模型不可用"}})
                continue
            model = value
        elif subtype == "apply_flag_settings":
            settings = request.get("settings") or {}
            if settings.get("fastMode") is True:
                fast_opt_in = True
        elif subtype == "interrupt":
            uuid = pending_interrupt_uuid
            if uuid:
                cancelled.add(uuid)
                emit({"type": "command_lifecycle", "command_uuid": uuid,
                      "state": "cancelled"})
        emit({"type": "control_response", "response": {
            "subtype": "success", "request_id": obj["request_id"], "response": {}}})
    elif typ == "control_response":
        response = obj.get("response") or {}
        if response.get("request_id") == "req-1":
            decision = (response.get("response") or {}).get("behavior")
            uuid, pending_permission_uuid = pending_permission_uuid, None
            complete(uuid, "decision:" + str(decision))
    elif typ == "user":
        init = {"type": "system", "subtype": "init", "session_id": "fake-session",
                "slash_commands": ["/compact", "model", "/review", "/model", "/fast"]}
        if fast_on:
            init["fast_mode_state"] = "on"
        elif not fast_opt_in:
            # 模拟真实 CLI：SDK 模式未 opt-in 时每回合都报 sdk_opt_in_required
            init["fast_mode_state"] = "off"
            init["fast_mode_disabled_reason"] = "sdk_opt_in_required"
        else:
            init["fast_mode_state"] = "off"
        emit(init)
        text = obj["message"]["content"]
        uuid = obj.get("uuid")
        # 真实 CLI 会先发 queued，再发 started
        emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "queued"})
        emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "started"})
        if text in ("/fast on", "/fast off"):
            # 复现真实行为：忙时命令先进 CLI 队列，等当前回合结束才执行；
            # 无 opt-in 时 /fast 被拒（result 仍带状态帧）；/fast off 顺带
            # 清掉 opt-in；result 带 fast_mode_state
            deadline = time.time() + 30
            while turn_busy.is_set() and time.time() < deadline:
                time.sleep(0.02)
            want = text.endswith("on")
            result = {"type": "result", "session_id": "fake-session",
                      "fast_mode_state": "off"}
            if want and not fast_opt_in:
                result["result"] = ("Fast mode unavailable: Fast mode is not "
                                    "available in the Agent SDK")
                result["fast_mode_disabled_reason"] = "sdk_opt_in_required"
            else:
                fast_on = want
                if not want:
                    fast_opt_in = False
                result["result"] = "Fast mode ON" if want else "Fast mode OFF"
                if want:
                    result["fast_mode_state"] = "on"
            emit(result)
            emit({"type": "command_lifecycle", "command_uuid": uuid,
                  "state": "completed"})
        if text.startswith("permission"):
            pending_permission_uuid = uuid
            emit({"type": "control_request", "request_id": "req-1", "request": {
                "subtype": "can_use_tool", "tool_name": "Edit", "tool_use_id": "tool-1",
                "input": {"file_path": "/tmp/a", "old_string": "old", "new_string": "new"},
                "permission_suggestions": [{"type": "addRules", "behavior": "allow",
                                            "destination": "session",
                                            "rules": [{"toolName": "Edit"}]}],
                "decision_reason": "writes a file"}})
        elif text == "ask":
            # AskUserQuestion 的 can_use_tool 兜底路径（PreToolUse hook 未生效时）
            pending_permission_uuid = uuid
            emit({"type": "control_request", "request_id": "req-1", "request": {
                "subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                "tool_use_id": "ask-1",
                "input": {"questions": [
                    {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]}]},
                "permission_suggestions": [],
                "decision_reason": "asks the user"}})
        elif text == "plan":
            pending_permission_uuid = uuid
            emit({"type": "control_request", "request_id": "req-1", "request": {
                "subtype": "can_use_tool", "tool_name": "ExitPlanMode",
                "tool_use_id": "plan-1",
                "input": {"plan": "# Plan\nDo the thing"},
                "permission_suggestions": []}})
        elif text == "api-retry":
            emit({"type": "system", "subtype": "api_retry", "attempt": 1,
                  "max_retries": 10, "error_status": 503,
                  "error": "server_error"})
            complete(uuid, "retried-ok")
        elif text == "lifecycle-first":
            emit({"type": "command_lifecycle", "command_uuid": uuid,
                  "state": "completed"})
            def late_result():
                time.sleep(0.05)
                emit({"type": "result", "session_id": "fake-session",
                      "result": "lifecycle-first"})
            threading.Thread(target=late_result, daemon=True).start()
        elif text == "stream":
            for chunk in ("你", "好", "世界"):
                emit({"type": "stream_event", "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": chunk}}})
            complete(uuid, "stream-done")
        elif text in ("slow", "interrupt-me"):
            pending_interrupt_uuid = uuid
            threading.Thread(target=slow_turn, args=(uuid, text), daemon=True).start()
        else:
            complete(uuid, str(model) + ":" + text)