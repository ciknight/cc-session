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


def emit(obj):
    with _write_lock:
        print(json.dumps(obj), flush=True)


def complete(uuid, text):
    emit({"type": "result", "session_id": "fake-session", "result": text})
    emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "completed"})


def slow_turn(uuid, text):
    time.sleep(5 if text == "interrupt-me" else 0.15)
    if uuid in cancelled:
        return
    complete(uuid, str(model) + ":" + text)


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
        emit({"type": "system", "subtype": "init", "session_id": "fake-session",
              "slash_commands": ["/compact", "model", "/review", "/model"]})
        text = obj["message"]["content"]
        uuid = obj.get("uuid")
        # 真实 CLI 会先发 queued，再发 started
        emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "queued"})
        emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "started"})
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