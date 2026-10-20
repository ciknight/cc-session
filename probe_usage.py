"""探测 litecore 端点上 glm-5.3 / glm-5.3-flash 的响应是否带 usage 字段。

用法（在自己的 PowerShell 里，不经过 Claude）：
    python probe_usage.py

判定：
    - 两个模型、流式/非流式都打印 usage -> flash 可保留在小模型档；
    - flash 的 usage 为 None 或请求报错     -> 把小模型档改回 glm-5.3[1m]（方案 A）。
"""
import json
import urllib.request

CFG = r"F:\code\claude-sessions\.claude\settings.local.json"


def probe(model, stream=False):
    payload = {"model": model, "max_tokens": 16,
               "messages": [{"role": "user", "content": "ping"}]}
    if stream:
        payload["stream"] = True
    req = urllib.request.Request(
        json.load(open(CFG, encoding="utf-8"))["env"]["ANTHROPIC_BASE_URL"].rstrip("/")
        + "/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                 "authorization": "Bearer "
                 + json.load(open(CFG, encoding="utf-8"))["env"]["ANTHROPIC_AUTH_TOKEN"]})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - 探测脚本，打印一切失败
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
        print(f"{model} stream={stream} -> 请求失败 {getattr(e, 'code', '')} "
              f"{type(e).__name__} {detail}")
        return
    if not stream:
        body = json.loads(raw)
        print(f"{model} stream=False -> usage={body.get('usage')} "
              f"stop={body.get('stop_reason')}")
        return
    # 流式：看 message_start 里 message.usage 和 message_delta 里 usage
    start_usage = delta_usage = None
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "message_start":
            start_usage = evt.get("message", {}).get("usage")
        elif evt.get("type") == "message_delta":
            delta_usage = evt.get("usage")
    print(f"{model} stream=True  -> message_start.usage={start_usage} "
          f"message_delta.usage={delta_usage}")


for m in ("glm-5.3[1m]", "glm-5.3-flash[1m]"):
    probe(m, stream=False)
    probe(m, stream=True)
