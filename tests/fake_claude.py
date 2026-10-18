"""测试专用 stream-json 进程，不连接模型服务。"""
import json
import sys
import time

model = None
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "--model" and i + 1 < len(args):
        model = args[i + 1]
    elif arg.startswith("--model="):
        model = arg.split("=", 1)[1]


def emit(obj):
    print(json.dumps(obj), flush=True)


for line in sys.stdin:
    obj = json.loads(line)
    if obj["type"] == "control_request":
        request = obj["request"]
        if request["subtype"] == "set_model":
            value = request.get("model")
            if value == "timeout":
                continue
            if value == "reject":
                emit({"type": "control_response", "response": {
                    "subtype": "error", "request_id": obj["request_id"], "error": "模型不可用"}})
                continue
            model = value
        emit({"type": "control_response", "response": {
            "subtype": "success", "request_id": obj["request_id"], "response": {}}})
    elif obj["type"] == "user":
        emit({"type": "system", "subtype": "init", "session_id": "fake-session"})
        text = obj["message"]["content"]
        if text == "slow":
            time.sleep(0.15)
        emit({"type": "result", "session_id": "fake-session", "result": str(model) + ":" + text})
