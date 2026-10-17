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
- 可用 `CLAUDE_CONFIG_DIR` 环境变量指向其他 Claude 配置目录做测试。

## 架构

单文件服务 `monitor.py`（约 500 行）+ Jinja 模板 `templates/index.html` + 本地前端依赖
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
4. **远程发消息/托管**（`send_message` + `Hosted`）：`POST /sessions/<id>/message` 用
   `claude -p --resume` 跑一轮（异步 job，`GET /jobs/<id>` 查询）；`POST /hosted` 启动
   stream-json 双向的常驻 claude 进程，消息写 stdin、忙时排队，已托管会话的
   message 请求自动路由到常驻进程。接口无鉴权，不要暴露到不可信网络。

### 关键设计决策

- **会话以文件路径为键**（`Monitor.sessions`）：同一 session id 可能出现在多个项目目录
  （恢复/移动过），按 id 键控会让两个文件的读取 offset 互相干扰。对外接口用
  `by_sid`/`unique_sessions` 按 id 去重（取最近活跃的）。
- **任务清单双来源**：从转录里的 `TaskCreate`/`TaskUpdate` 工具调用推断，但
  `~/.claude/tasks/<sid>/*.json` 目录存在时以它为准（`_merge_task_dirs`）。
- **对话消息按需增量提取**（`parse_msg_line`）：不随轮询解析，只在请求
  `/sessions/<id>/messages` 时解析；`Monitor.msg_cache` 记录每个文件的读取 offset、
  半行 buffer、已解析消息和序列化字节，后续请求只解析新增部分；
  跳过 sidechain（子代理对话）和非错误工具结果。
- **worktree 归并**（`project_root`）：`<proj>/.claude/worktrees/<name>` 视为属于 `<proj>`，
  看板按项目分组依赖此逻辑。
- **状态机**：`working`（有活动且助手未完成回复）/ `idle`（`stop_reason=end_turn`）/
  `stale`（超过 `--stale` 秒无更新，在 `summary()` 输出时动态计算，不存状态）。

### 前端

`templates/index.html` 是自包含 SPA：左侧项目列表、右侧会话卡片/对话视图，
对话用 marked + DOMPurify 渲染 markdown（用户气泡 / 助手 markdown / 工具调用紧凑行），
自动刷新间隔存在 localStorage，对话视图仅在文件 size 变化时重渲染并保持滚动位置。
