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
| `--claude-bin` | `claude` | Claude Code CLI 路径 |
| `--claude-args` | 空 | 透传给每个 Claude 子进程的参数，例如 `--model sonnet` |
| `--msg-timeout` | `600` | 一次性消息执行超时（秒） |
| `--scan-subagents` | 关闭 | 启动时扫描子代理转录；默认懒加载，仅在 `?include_subagents=1` 或请求子代理 id 时读取 |

## 网页模型与项目配置

- 会话详情中的“模型选择”按 session ID 保存。选项包含默认和自定义模型 ID，以及仅从当前会话（新建时为目标项目）`cwd/.claude/settings.local.json` 的 `env` 中读取的 `OPUS_MODEL`、`SONNET_MODEL`、`HAIKU_MODEL`、`FABLE_MODEL` 对应值；各字段缺失时兼容 `ANTHROPIC_DEFAULT_<系列>_MODEL`。选项显示“系列: 模型 ID”；不读取其他 profile 或全局配置，不将通用 `env.ANTHROPIC_MODEL` 或顶层 `model` 当作系列模型。local 文件不存在、损坏或无有效系列字段时，仅保留默认和自定义选项。普通（非托管）会话将模型 ID 作为后续消息的 `--model` 参数；托管会话在轮次边界通过 `set_model` 切换。只保存和发送模型 ID，不应用 profile 的 local 配置，也不控制其他终端里的进程。
- local 配置解析只向前端提供模型选项的 `name` 和 `model` 字段；配置正文和 credentials 不传到前端。“最近响应”来自会话转录，表示最后一次实际记录的模型，与下一条消息的选择分开显示。显式 Web 选择优先于服务级 `--claude-args` 中的 `--model`；清除 Web 选择后恢复服务配置/Claude 默认规则。
- 托管会话在轮次边界通过 stream-json `set_model` 控制请求切换。控制失败或超时后不会把消息悄悄发给旧模型，托管进程需要退出并重新托管。
- cc-switch local 配置控件仅在项目会话列表展示（会话详情和新建会话页不显示），且仅在已安装并可通过 PATH 找到 `cc-switch` 时启用。使用 cc-switch 的 `local <profile>` 命令写入显示的准确目录下 `.claude/settings.local.json`；服务端仅列出 profile 文件名供配置切换，模型选项则读取当前目录 local 配置，不会把配置正文或 credentials 传到前端。
- cc-switch local 会整体覆盖目标文件，不合并原文件；应用前需确认，原文件会先备份到当前 Claude 配置目录下 `claude-sessions/backups/`。应用失败时保留备份，刷新检查磁盘状态后可人工恢复。配置目录不是默认 `~/.claude` 时会禁用切换，因为 cc-switch 不支持 `CLAUDE_CONFIG_DIR`。
- cc-switch 调用失败时，服务终端会输出 `[cc-switch]` 安全诊断（失败阶段、退出码、系统错误号和可识别的原因类别）；浏览器也会提示非零退出码。CLI 原始输出和异常原文可能包含凭据，默认不记录、不返回。
- local 是目录级配置，同目录的其他会话也会受到后续启动影响。已有托管进程不会自动重启；应用后需退出托管并重新托管才能加载。会话模型选择、服务环境变量仍可能覆盖 profile 的模型设置。
- 会话详情和新建会话共用同一个输入对话框：Enter 发送，Shift+Enter 换行；输入法 composition 期间按 Enter 不会发送。
- 主版服务默认监听 `0.0.0.0` 且没有身份认证。只能在可信网络使用；模型/配置接口不是凭据管理接口。不要将服务暴露给不可信网络。

## 托管、审批与发送

- 托管会话的审批模式默认为 `auto`（交给 CLI 权限模式）。已有明确保存过 `ask`/`full` 的会话继续使用保存值；可在会话详情切换“询问”或“全部允许”。
- 会话处于 `working` 时网页仍可发送消息：已托管会话进入常驻进程队列；非托管会话的“工作状态下也发送”默认勾选，会用 `force` 绕过 working 检查。非托管会话的一次性消息也会按会话排队（上限 32 条），上一条结束后自动执行下一条。
- 新建会话的“托管”模式也会读取对话框里的审批选择，并作为首轮 `/hosted` 的 `approval` 提交。
- 中断按钮只对托管且正在执行的会话启用；排队中的消息会随中断一起取消。

## 接口

| 接口 | 说明 |
| --- | --- |
| `GET /`、`GET /p/...` | 网页看板：左侧项目列表，右侧会话列表/对话视图，自动刷新可开关；`/p/<项目>`、`/p/<项目>/s/<会话>`、`/p/<项目>/new` 支持直接访问与浏览器前进后退 |
| `GET /sessions` | 所有会话概览（`?full=1` 时含任务、子代理、最近事件等完整信息；`?include_subagents=1` 触发子代理懒加载） |
| `GET /sessions/<id>` | 按 id 取单个会话完整信息 |
| `GET /sessions/<id>/messages` | 按需解析该会话的完整对话（带缓存，跳过 sidechain 子代理对话） |
| `GET /static/<file>` | 前端静态资源（marked / DOMPurify） |
| `GET /events` | SSE 实时事件流（`?session=<id>` 可过滤） |
| `GET /healthz` | 健康检查 |
| `POST /sessions/<id>/model` | 保存会话 Web 模型选择：`{"model":"sonnet"}`；传 `null` 清除覆盖 |
| `GET /cc-switch?cwd=<目录>` | 获取该已监控目录的 cc-switch 可用状态、profile 名称、仅含 `name`/`model` 的安全模型选项及目标文件摘要 |
| `POST /cc-switch/local` | 确认后备份并调用已安装 cc-switch：`{"cwd":"<目录>","profile":"<名称>","confirm":true,"revision":"<摘要>"}` |

现有 `POST /sessions/<id>/message`、`POST /new`、`POST /hosted` 可选接受 `model` 字符串或 `null`。省略时使用该会话已保存的 Web 选择；新会话创建时通过该字段指定首轮模型。任务响应中的 `requested_model` 是本次提交的选择，不能当作服务端确认的实际响应模型。

## 会话信息

每个会话展示的信息均解析自转录文件：

- 标题（ai-title）、项目目录（cwd）、git 分支、模型、Claude 版本
- 首/末事件时间（时长）、用户/助手消息数
- token 用量（input / output / cache）
- 工具调用统计、子代理（Agent 工具）列表
- 任务清单（TaskCreate / TaskUpdate 或 `~/.claude/tasks/<sid>` 目录）

会话状态：

- `working` — 最近有活动，且最后一个事件不是"助手完成回复"；Agent 工具调用在收到与其 tool-use ID 匹配的 `tool_result` 前均视为活动，因此主助手轮次结束后仍可能处于 working。
- `idle` — 助手已完成回复（`stop_reason=end_turn`），等待用户输入，且没有未完成的 Agent 工具调用。
- `stale` — 超过 `--stale` 秒没有任何更新（动态计算，不存状态）；即使仍有未完成的 Agent 工具调用，若活动事件超过 stale 阈值，状态也为 stale。

会话详情中的子代理记录按最新优先展示。

## 架构

服务入口 `monitor.py` + 扫描缓存模块 `scan_cache.py` + 会话偏好模块 `session_preferences.py` + 已安装 cc-switch 适配层 `cc_switch.py` + Jinja 模板 `templates/index.html` + 本地前端依赖 `static/marked.min.js`、`static/purify.min.js`（不依赖外网 CDN）。

### 数据流

1. **轮询线程**（`Monitor.run` → `scan_once`）：按 `--interval` 秒扫描 `CLAUDE_DIR/projects/*/*.jsonl`，
   每个 `Session` 维护文件 offset 增量读取，半行留在 buffer 下次拼接。子代理转录默认不扫描；
   `?include_subagents=1` 或请求 `<parent>::agent-*` 时按需加载（`scan_subagents_once`/`ensure_subagent`）。
2. **扫描缓存**（`scan_cache.py`）：每个转录的 offset、末尾 4 KiB 校验值和累计状态保存在
   `CLAUDE_DIR/claude-sessions/scan-cache.json`。重启时大小和末尾校验一致就直接恢复，只解析追加字节；
   文件截断、重写或缓存损坏时回退全量解析。默认只缓存主会话，避免子代理全量扫描时反复写大文件。
3. **行解析**（`handle_line`）：逐行 JSON 解析，更新 `Session` 的 cwd/git 分支/模型/token 用量/
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
  `~/.claude/tasks/<sid>/*.json` 目录存在时以它为准（`_merge_task_dirs`，按 UTF-8 读取）。
- **子代理默认懒加载**：子代理转录通常占用大部分磁盘和首次扫描时间，而默认看板不展示它们；
  显式请求时再扫描，父会话的 `subagent_count` 仍来自主转录里的 Agent 工具调用。
- **扫描缓存以文件路径为键**：与 `Monitor.sessions` 一致，同一 sid 出现在多个项目目录时不会互相污染；
  缓存只保存增量状态，不改写转录。
- **对话消息按需增量提取**（`parse_msg_line`）：不随轮询解析，只在请求
  `/sessions/<id>/messages` 时增量读取新增内容，保留半行并缓存序列化结果（`Monitor.msg_cache`）；
  跳过 sidechain（子代理对话）和非错误工具结果。
- **会话模型选择独立于转录**：`session_preferences.py` 按 sid 保存 Web 覆盖；`Session.model` 仍表示最近实际响应模型。
- **local profile 只通过已安装 cc-switch 应用**：`cc_switch.py` 为模型选项解析 profile JSON 并只输出 `name`/`model` 字段；local 配置仍由 cc-switch 应用，服务不自行实现 profile 合并，也不把配置正文或 credentials 传到前端。
- **项目归并**：`<proj>/.claude/worktrees/<name>` 和 `<proj>/.worktrees/<name>` 视为属于 `<proj>`；
  `project_map()` 进一步把子目录里的会话归并到最外层祖先项目（如 `<proj>/web` → `<proj>`），
  看板按项目分组依赖此逻辑。

### 前端

`templates/index.html` 是自包含 SPA：左侧项目列表、右侧会话卡片/对话视图，
对话用 marked + DOMPurify 渲染 markdown（用户气泡 / 助手 markdown / 工具调用紧凑行），
自动刷新间隔存在 localStorage，对话视图仅在文件 size 变化时重渲染并保持滚动位置。
会话页和新建会话页共用 `composerHtml` 输入对话框（Enter 发送、Shift+Enter 换行，
新建会话的托管模式也可选审批模式）；
工具调用、子代理、任务等 `<details>` 的展开状态记录在 `detailsOpen`，自动刷新重建后恢复，
并写入 `sessionStorage` 以兼容整页刷新。
