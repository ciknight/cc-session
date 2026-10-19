# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

监控 Claude Code 会话任务内容和状态的 Web 服务：轮询 `~/.claude/projects/<项目>/<sessionId>.jsonl`
转录文件，实时解析会话状态，提供 JSON API、SSE 事件流和网页看板。代码与文档均使用中文。

## 常用命令

```bash
uv run monitor.py                    # 启动服务（默认 http://0.0.0.0:8765）
uv run monitor.py --port 9000 --stale 600
uv run monitor.py --webhook http://localhost:3000/hook
```

- 依赖管理用 [uv](https://docs.astral.sh/uv/)：`uv add <pkg>` 添加依赖，改完 `pyproject.toml` 后 `uv lock`。
- 无测试框架、无 lint 配置；唯一依赖是 Jinja2，其余为标准库。
- 单元测试：`PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v`；
  前端测试：`node --test tests/test_frontend_controls.js`。
- 真实 CLI 权限冒烟（会调用模型）：`CLAUDE_SESSIONS_SMOKE=1 uv run python tests/smoke_permission_live.py`；
  AskUserQuestion 冒烟加 `SMOKE_ASK=1`。
- 可用 `CLAUDE_CONFIG_DIR` 环境变量指向其他 Claude 配置目录做测试。

## 架构

单文件服务 `monitor.py`（约 1900 行）+ Jinja 模板 `templates/index.html` + 本地前端依赖
`static/marked.min.js`、`static/purify.min.js`（不依赖外网 CDN）。

### 数据流

1. **轮询线程**（`Monitor.run` → `scan_once`）：按 `--interval` 秒扫描 `CLAUDE_DIR/projects/*/*.jsonl`，
   每个 `Session` 维护文件 offset 增量读取，半行留在 buffer 下次拼接。
2. **行解析**（`handle_line`）：逐行 JSON 解析，更新 `Session` 的 cwd/git 分支/模型/token 用量/
   工具统计/任务清单/状态等。事件经 `emit` 分发给 SSE 订阅者和可选 webhook；
   **首次扫描只回放不推送**（`first_scan_done` 标志）。
3. **HTTP 层**（`make_handler`，`ThreadingHTTPServer` + `BaseHTTPRequestHandler`）：
   `GET /` 渲染看板模板，`/sessions`、`/sessions/<id>`、`/sessions/<id>/messages` 返回 JSON，
   `/events` 是 SSE 流（连接先发 `snapshot`，随后推增量），`/static/` 提供静态文件。
   `/sessions/<id>/messages` 支持 `limit`（默认 100）、`before`、`from` 分页。
   托管进程通过 `POST /hook/PreToolUse` 接收 AskUserQuestion（仅回环 + 令牌）。
4. **远程发消息/托管**（`send_message` + `Hosted`）：`POST /sessions/<id>/message` 用
   `claude -p --resume` 跑一轮（异步 job，`GET /jobs/<id>` 查询）；`POST /hosted` 启动
   stream-json 双向的常驻 claude 进程，消息写 stdin、忙时排队，已托管会话的
   message 请求自动路由到常驻进程。托管进程带 `--permission-prompt-tool stdio`，
   权限请求经 `/interactions` 审批；`POST /sessions/<id>/interrupt` 中断当前回合。
   接口无鉴权，不要暴露到不可信网络。

### 关键设计决策

- **会话以文件路径为键**（`Monitor.sessions`）：同一 session id 可能出现在多个项目目录
  （恢复/移动过），按 id 键控会让两个文件的读取 offset 互相干扰。对外接口用
  `by_sid`/`unique_sessions` 按 id 去重（取最近活跃的）。
- **任务清单双来源**：从转录里的 `TaskCreate`/`TaskUpdate` 工具调用推断，但
  `~/.claude/tasks/<sid>/*.json` 目录存在时以它为准（`_merge_task_dirs`）。
- **对话消息按需增量提取**（`parse_msg_line`）：不随轮询解析，只在请求
  `/sessions/<id>/messages` 时解析；`Monitor.msg_cache` 记录每个文件的读取 offset、
  已解析消息和 tool_use 索引，后续请求只解析新增部分。超过 2 MiB 的转录首次只解析
  尾部窗口，`before` 翻页时按 1 MiB 块向前加载并前插；每个会话有独立解析锁。
  跳过 sidechain（子代理对话）。工具结果合并行级 `toolUseResult`（diff/stdout/图片），
  `isMeta` 分 boilerplate / sourceToolUseID / origin 注入三种语义。
- **子代理会话**：额外扫描 `projects/<proj>/<parent>/subagents/agent-*.jsonl`，
  id 为 `<parent>::agent-<hash>`，默认不在 `/sessions` 返回（`?include_subagents=1`），
  只读、不能发消息或托管。
- **worktree 归并**（`project_root`）：`<proj>/.claude/worktrees/<name>` 视为属于 `<proj>`，
  看板按项目分组依赖此逻辑。
- **状态机**：`working`（有活动且助手未完成回复）/ `idle`（`system/turn_duration`；
  该会话从未出现该标记时才回退到 `stop_reason=end_turn`）/ `stale`（超过 `--stale`
  秒无内容事件，在 `summary()` 输出时动态计算）。
- **双时钟**：`last_input_ts` 只由真实用户输入推进（排除 tool_result、isMeta、
  origin/promptSource 注入、中断标记），`last_event_ts` 是最后一条带时间戳的转录事件；
  列表按 `last_input_ts` 排序、回退 `last_event_ts`。
- **托管审批**：模式为 `ask`/`auto`/`full`，持久化在
  `~/.claude/claude-sessions/approvals.json`；`ask` 等待网页响应（`--permission-timeout`
  默认 300 秒，超时自动拒绝），`auto` 交给 CLI 权限模式，`full` 由服务自动放行。
  回合生命周期以 `command_lifecycle` 为准，`result` 作为旧版兜底；
  `queued/started/running/released` 是非终态，只有
  `completed/cancelled/discarded/refused/dropped` 结束回合。
- **流式文本**：托管进程带 `--include-partial-messages`；`stream_event` 的
  `text_delta` 按 job 聚合，约 80ms 节流发 `turn_delta`（transient，不进 recent/webhook），
  回合结束发最终文本并清空。
- **网页问答**：托管启动时用内联 `--settings` 注册 AskUserQuestion 的 PreToolUse hook，
  command 为 `python monitor.py hook --port <port> --token <token>`；
  `/hook/PreToolUse` 只接受回环地址和每次启动随机生成的令牌。若用户已通过
  `--claude-args` 提供内联 `--settings`，会把 hook 合并进去；指向文件时跳过并提示。
  hook 客户端用二进制 stdin/stdout（避免 Windows 代码页问题）并绕过 HTTP 代理。
- **前端实时更新**：EventSource 全局 + 单会话两路，各自维护 `seq`；连接先收 `snapshot`，
  断线重连后全量对账；SSE 事件触发节流刷新，保留定时轮询兜底。
  对话只加载最近 100 条，向上滚动加载更早；`cursor` 记录已加载末尾，
  服务端前插旧块时用响应里的 `shift` 调整，避免增量漏消息。

### 前端

`templates/index.html` 是自包含 SPA：左侧项目列表、右侧会话卡片/对话视图，
对话用 marked + DOMPurify 渲染 markdown（用户气泡 / 助手 markdown / 可折叠工具结果与 diff），
自动刷新间隔存在 localStorage，对话视图仅在文件 size 变化时重渲染并保持滚动位置。
对话视图顶部显示待审批卡片，发送区提供中断按钮和审批模式选择。
