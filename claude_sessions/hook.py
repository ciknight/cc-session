"""Claude Code PreToolUse hook 的客户端：转发给本机服务并回写结果。"""
import argparse
import sys
import urllib.request


def hook_client(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--event", default="PreToolUse")
    ap.add_argument("--timeout", type=int, default=None,
                    help="等待服务端审批结果的秒数；0 或缺省表示无限等待")
    args = ap.parse_args(argv)
    # 二进制读写，避免 Windows 管道按本地代码页转换中文
    payload = sys.stdin.buffer.read()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/hook/{args.event}",
            data=payload,
            headers={"Content-Type": "application/json",
                     "X-Monitor-Token": args.token})
        # 绕过 http_proxy，避免把本地请求和令牌发给代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        timeout = args.timeout if args.timeout and args.timeout > 0 else None
        with opener.open(req, timeout=timeout) as resp:
            sys.stdout.buffer.write(resp.read())
    except Exception:
        # 服务不可达时 fail-open，让 CLI 回落到自身行为
        sys.stdout.buffer.write(b"{}")
    return 0
