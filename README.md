# claude-sessions 监控

监控 Claude Code 会话任务内容和状态的 Web 服务。轮询 `~/.claude/projects/<项目>/<sessionId>.jsonl`
转录文件，实时解析会话状态，提供 JSON API、SSE 事件流和网页看板。

## 快速开始

```bash
uv run monitor.py                    # 启动服务（默认 http://0.0.0.0:8765）
uv run monitor.py --port 9000 --stale 600
uv run monitor.py --webhook http://localhost:3000/hook
```

依赖管理使用 [uv](https://docs.astral.sh/uv/)：`uv add <pkg>` 添加依赖，改完 `pyproject.toml` 后执行 `uv lock`。
唯一第三方依赖是 Jinja2，其余均为 Python 标准库。

可用 `CLAUDE_CONFIG_DIR` 环境变量指向其他 Claude 配置目录做测试：

```bash
CLAUDE_CONFIG_DIR=/path/to/.claude uv run monitor.py
```

## 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8765` | 监听端口 |
| `--interval` | `1.0` | 轮询间隔（秒） |
| `--refresh` | `3.0` | 网页自动刷新间隔（秒） |
| `--stale` | `300` | 多少秒无更新视为 stale |
| `--webhook` | 无 | 把每个事件 POST 到该 URL |

## 接口

| 接口 | 说明 |
| --- | --- |
| `GET /` | 网页看板：左侧项目列表，右侧会话列表/对话视图，自动刷新可开关 |
| `GET /sessions` | 所有会话概览（`?full=1` 时含任务、子代理、最近事件等完整信息） |
| `GET /sessions/<id>` | 按 id 取单个会话完整信息 |
| `GET /sessions/<id>/messages` | 按需解析该会话的完整对话（带缓存，跳过 sidechain 子代理对话） |
| `GET /static/<file>` | 前端静态资源（marked / DOMPurify） |
| `GET /events` | SSE 实时事件流（`?session=<id>` 可过滤） |
| `GET /healthz` | 健康检查 |

## 会话信息

每个会话展示的信息均解析自转录文件：

- 标题（ai-title）、项目目录（cwd）、git 分支、模型、Claude 版本
- 首/末事件时间（时长）、用户/助手消息数
- token 用量（input / output / cache）
- 工具调用统计、子代理（Agent 工具）列表
- 任务清单（TaskCreate / TaskUpdate 或 `~/.claude/tasks/<sid>` 目录）

会话状态：

- `working` — 最近有活动，且最后一个事件不是"助手完成回复"
- `idle` — 助手已完成回复（`stop_reason=end_turn`），等待用户输入
- `stale` — 超过 `--stale` 秒没有任何更新（动态计算，不存状态）

## 架构

单文件服务 `monitor.py` + Jinja 模板 `templates/index.html` + 本地前端依赖
`static/marked.min.js`、`static/purify.min.js`（不依赖外网 CDN）。

### 数据流

1. **轮询线程**（`Monitor.run` → `scan_once`）：按 `--interval` 秒扫描 `CLAUDE_DIR/projects/*/*.jsonl`，
   每个 `Session` 维护文件 offset 增量读取，半行留在 buffer 下次拼接。
2. **行解析**（`handle_line`）：逐行 JSON 解析，更新 `Session` 的 cwd/git 分支/模型/token 用量/
   工具统计/任务清单/状态等。事件经 `emit` 分发给 SSE 订阅者和可选 webhook；
   **首次扫描只回放不推送**（`first_scan_done` 标志）。
3. **HTTP 层**（`make_handler`，`ThreadingHTTPServer` + `BaseHTTPRequestHandler`）：
   `GET /` 渲染看板模板，`/sessions`、`/sessions/<id>`、`/sessions/<id>/messages` 返回 JSON，
   `/events` 是 SSE 流，`/static/` 提供静态文件。

### 关键设计决策

- **会话以文件路径为键**（`Monitor.sessions`）：同一 session id 可能出现在多个项目目录
  （恢复/移动过），按 id 键控会让两个文件的读取 offset 互相干扰。对外接口用
  `by_sid`/`unique_sessions` 按 id 去重（取最近活跃的）。
- **任务清单双来源**：从转录里的 `TaskCreate`/`TaskUpdate` 工具调用推断，但
  `~/.claude/tasks/<sid>/*.json` 目录存在时以它为准（`_merge_task_dirs`）。
- **对话消息按需提取**（`extract_messages`）：不随轮询解析，只在请求
  `/sessions/<id>/messages` 时读整个文件，按文件大小缓存（`Monitor.msg_cache`）；
  跳过 sidechain（子代理对话）和非错误工具结果。
- **项目归并**：`<proj>/.claude/worktrees/<name>` 和 `<proj>/.worktrees/<name>` 视为属于 `<proj>`；
  `project_map()` 进一步把子目录里的会话归并到最外层祖先项目（如 `<proj>/web` → `<proj>`），
  看板按项目分组依赖此逻辑。

### 前端

`templates/index.html` 是自包含 SPA：左侧项目列表、右侧会话卡片/对话视图，
对话用 marked + DOMPurify 渲染 markdown（用户气泡 / 助手 markdown / 工具调用紧凑行），
自动刷新间隔存在 localStorage，对话视图仅在文件 size 变化时重渲染并保持滚动位置。
