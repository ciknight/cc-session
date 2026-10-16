# claude-sessions

监控 Claude Code 会话任务内容和状态的 Web 服务。Jinja2 渲染看板页面，用 [uv](https://docs.astral.sh/uv/) 管理 Python 环境。

## 数据来源

- `~/.claude/projects/<项目>/<sessionId>.jsonl` — 会话转录，逐行追加
- `~/.claude/tasks/<sessionId>/*.json` — 任务清单（如果存在，作为补充校正）

## 运行

```bash
uv run monitor.py                    # 监听默认目录，http://127.0.0.1:8765
uv run monitor.py --port 9000 --stale 600
uv run monitor.py --webhook http://localhost:3000/hook   # 事件同时 POST 出去
```

## 网页看板

- 左侧项目列表，点击后右侧显示该项目的所有会话卡片
- 点击会话卡片进入会话视图：信息面板（模型、git 分支、版本、消息数、token 用量、
  工具调用统计、子代理列表、任务清单）+ 完整对话渲染
- 对话按聊天样式渲染：用户气泡 + 助手 markdown（marked + DOMPurify，本地静态文件不依赖外网），
  工具调用和错误以紧凑行展示；子代理 sidechain 对话自动过滤
- worktree 目录（`<项目>/.claude/worktrees/<名字>`）自动归并到主项目
- 自动刷新开关 + 刷新间隔选择（2s/3s/5s/10s），设置保存在 localStorage；
  对话视图下文件有更新才重渲染，并保持滚动位置
- 老的 `ai-title` 乱码数据自动回退为首条用户消息作为标题

## HTTP 接口

| 接口 | 说明 |
|---|---|
| `GET /` | 网页看板（Jinja 模板 `templates/index.html`） |
| `GET /sessions` | 所有会话概览（按 session id 去重）；加 `?full=1` 返回完整信息 |
| `GET /sessions/<id>` | 按 id 取单个会话完整信息（任务、子代理、工具统计、最近事件） |
| `GET /sessions/<id>/messages` | 按需解析完整对话（带缓存；跳过 sidechain 和工具结果，工具错误保留） |
| `GET /static/<file>` | 前端静态资源（`static/` 目录，防目录穿越） |
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
| `--interval` | `1.0` | 后端轮询转录文件间隔（秒） |
| `--refresh` | `3.0` | 网页默认自动刷新间隔（秒） |
| `--stale` | `300` | 多少秒无更新视为 stale |
| `--webhook` | 无 | 把每个事件 POST 到该 URL |
