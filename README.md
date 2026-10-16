# claude-sessions

监控 Claude Code 会话任务内容和状态的 Web 服务。纯标准库实现，零第三方依赖，用 [uv](https://docs.astral.sh/uv/) 管理 Python 环境。

## 数据来源

- `~/.claude/projects/<项目>/<sessionId>.jsonl` — 会话转录，逐行追加
- `~/.claude/tasks/<sessionId>/*.json` — 任务清单（如果存在，作为补充校正）

## 运行

```bash
uv run monitor.py                    # 监听默认目录，http://127.0.0.1:8765
uv run monitor.py --port 9000 --stale 600
uv run monitor.py --webhook http://localhost:3000/hook   # 事件同时 POST 出去
```

## HTTP 接口

| 接口 | 说明 |
|---|---|
| `GET /` | 网页看板：列出所有会话及任务，每 2 秒自动刷新 |
| `GET /sessions` | 所有会话的完整信息（状态、消息、任务列表、最近事件）；加 `?brief=1` 只返回概览 |
| `GET /sessions/<id>` | 按 id 取单个会话 |
| `GET /events` | SSE 实时事件流（`?session=<id>` 可过滤） |
| `GET /healthz` | 健康检查 |

## 会话状态

- `working` — 最近有活动，且最后一个事件不是“助手完成回复”
- `idle` — 助手已完成回复（`stop_reason=end_turn`），等待用户输入
- `stale` — 超过 `--stale` 秒没有任何更新

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8765` | 监听端口 |
| `--interval` | `1.0` | 轮询间隔（秒） |
| `--stale` | `300` | 多少秒无更新视为 stale |
| `--webhook` | 无 | 把每个事件 POST 到该 URL |
