# SSE 实时更新、消息分页、流式文本与 AskUserQuestion 设计

日期：2026-10-20
状态：已确认，待实现

## 1. 目标

在现有 `monitor.py` 单文件服务上实现两组能力：

1. **C：实时更新与长会话性能**
   - SSE 混合模式：连接快照 + 增量事件 + 低频轮询兜底；
   - 消息分页（`limit` / `before` / `from`）；
   - 前端渲染缓存与增量追加；
   - 托管回合的实时文本流（`turn_delta`）。
2. **E：交互完整性**
   - plan mode（`ExitPlanMode`）专用卡片；
   - `AskUserQuestion` 通过 `PreToolUse` hook 在网页上回答。

非目标：一次性任务中断、鉴权、PWA 推送、fork、搜索、定时任务。

## 2. 约束

- 保持单文件服务结构，不新增运行时依赖。
- 事件是提示，不是事实来源；任何丢失都必须能由一次全量查询修复。
- 首次扫描只回放不推送的现有行为保留。
- 转录解析和消息缓存仍按文件增量进行。
- 前端保持自包含 SPA，不引入构建工具。
- 所有动态内容使用 `esc()` 或文本节点渲染。

## 3. SSE 协议

### 3.1 连接与快照

`GET /events`（全局）和 `GET /events?session=<id>`（单会话）保持现有入口。

连接建立后立即发送一条 `snapshot` 事件：

```json
{
  "type": "snapshot",
  "seq": 123,
  "ts": 1760000000.0,
  "data": {
    "sessions": [ ...SessionSummary... ],
    "interactions": [ ...Pending... ],
    "live": {"session_id": "...", "active": true, "text": "partial assistant text"}
  }
}
```

- 全局快照包含全部可见会话（不含子代理）和所有 pending 审批。
- 单会话快照只包含该会话的 summary、pending 审批和 `live`。
- `live.active` 表示该会话当前有托管 job 在跑；`live.text` 是已聚合的流式文本。

### 3.2 事件格式

现有格式保持不变，增加 `seq`：

```json
{"ts": 1760000000.0, "seq": 124, "session": "<sid>", "type": "user_message", "data": {...}}
```

SSE 帧：

```
id: 124
event: user_message
data: {...}
```

心跳仍为每 15 秒 `: keepalive`。

### 3.3 事件类型

保留已有：

`session_discovered`、`user_message`、`assistant_message`、`tool_use`、`tool_error`、`turn_finished`、`subagent_start`、`task_created`、`task_updated`、`context_compacted`、`api_error`、`remote_message_sent/done`、`hosted_exited`、`hosted_turn_done`、`hosted_api_retry`、`hosted_foreign_turn`、`interaction_pending`、`interaction_resolved`、`turn_interrupted`。

新增：

| 类型 | data | 说明 |
|---|---|---|
| `snapshot` | 见 3.1 | 连接初始快照 |
| `turn_delta` | `{hosted, job, text}` | 流式文本增量（transient） |

`turn_delta` 是 transient：不写入 `Session.recent`，不触发 webhook，只发送给 SSE 订阅者。

### 3.4 发布与序号

- `Monitor` 维护单调递增的 `self.event_seq`，在 `emit` 中分配。
- `emit(sess, kind, data, transient=False)`：
  - transient 事件跳过 `sess.recent` 和 webhook；
  - 所有事件都推送给 SSE 订阅者。
- SSE 订阅者队列满时仍然静默丢弃；客户端通过重连快照修复。

## 4. 消息分页

### 4.1 API

`GET /sessions/<id>/messages`

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `limit` | 100 | 返回条数，最大 500 |
| `before` | 无 | 整数索引；返回 `[max(0,before-limit), before)` |
| `from` | 无 | 整数索引；返回 `[from, from+limit)` |

响应：

```json
{
  "id": "<sid>",
  "size": 123456,
  "total": 4200,
  "start": 4100,
  "end": 4200,
  "has_more": true,
  "messages": [ ... ]
}
```

- 无 `before`/`from` 时返回最后 `limit` 条。
- `before` 与 `from` 同时出现时 `before` 优先，返回 400。
- `total` 是当前缓存中的消息总数。
- `has_more` 表示 `start > 0`。
- 文件被截断时，缓存重置，响应从头开始；客户端发现 `total < 已知总数` 或 `size < 已知 size` 时全量重载。

### 4.2 缓存行为

- `Monitor.msg_cache[sid]` 继续保存完整解析结果 `msgs`。
- 分页只在序列化时切片，不改变增量解析逻辑。
- 流式文本不进入 `msgs`；它来自托管进程的 stdout。

## 5. 实时文本流

- 托管进程启动参数增加 `--include-partial-messages`。
- `_read_out` 处理 `type=stream_event`：
  - `event.type == "content_block_delta"` 且 `delta.type == "text_delta"`：追加到当前 job 的 `stream_text`。
  - `event.type == "content_block_start"` 且 block 为 text：同一 job 内继续累积（不重置）；新 job 开始时已清空。
  - 工具调用、thinking 等其他 block 忽略。
- 聚合与节流：
  - `Hosted` 维护 `stream_text`、`stream_job`、`last_delta_emit`。
  - 距上次 emit 超过 80ms 时发送 `turn_delta`（携带完整聚合文本）；否则只累积。
  - 回合结束（`command_lifecycle` 终态或 result 兜底）发送最后一次 `turn_delta`，然后清空。
- `Hosted.info()` 增加 `stream_text`、`streaming`。
- `/sessions/<id>` 详情增加 `hosted_stream_text`、`hosted_streaming`。
- 前端：
  - 收到本会话 `turn_delta` 时显示/更新一个 `.live-bubble`，内容用 `esc()`。
  - 收到 `turn_finished`、`hosted_turn_done`、`interaction_pending` 或对话刷新后，清除临时气泡，使用正式消息。
  - 只在一个流式气泡中显示，避免重复。

## 6. 前端

### 6.1 连接管理

- 页面加载时建立全局 `EventSource("/events")`。
- 打开会话时建立单会话 `EventSource("/events?session=<id>")`；切换会话时关闭旧的。
- 事件处理：
  - `snapshot`：替换本地 sessions/pending 数据，渲染当前视图。
  - 其他事件：按类型更新 dirty 标记。
- 刷新节流：
  - 列表：最多每 800ms 触发一次 `/sessions`。
  - 对话：最多每 500ms 触发一次 detail + messages 增量。
  - `turn_delta` 直接更新气泡，不触发 HTTP。
- 断线：
  - `es.onopen` 非首次连接时标记 dirty，并立即全量刷新；
  - 保留每 30 秒的低频轮询兜底；用户手动关闭自动刷新时不轮询。
- `document.hidden` 时不发起刷新，恢复可见时补一次。

### 6.2 渲染缓存

- 侧栏：`lastSidebarHtml` 不变则不写 DOM。
- 会话列表：`lastListHtml` 不变则不写 DOM；`bindCc` 只在首次渲染后调用。
- 对话：`lastConvSig = [id, total, size]` 不变则不重绘。
- runtime/info 面板沿用现有按需更新。

### 6.3 消息分页

- `convState = {id, total, size, hasMore, loadingEarlier}`。
- 内部消息数组按时间正序保存（旧 → 新）；渲染时反转为最新在前。
- 首次进入：`GET /messages?limit=100`，保存 `start/total`。
- 新消息：`GET /messages?from=<total>`，追加到内部数组末尾。
- 加载更早：`GET /messages?before=<start>&limit=100`，前插到内部数组开头。
- UI 在对话末尾（最旧消息下方）显示「加载更早」按钮；`has_more=false` 时隐藏。
- 加载更早时保持滚动位置（记录 `scrollHeight` 差）。

### 6.4 全局待审批

保留现有页头「待审批 N」按钮和会话卡片标记；SSE 的 `interaction_pending/resolved` 触发刷新。

## 7. Plan mode

- `_on_can_use_tool` 中 `tool_name == "ExitPlanMode"`：
  - `Interaction.kind = "plan"`；
  - `allow_always = False`；
  - `preview = {"kind": "text", "text": input.get("plan") or JSON}`。
- `Interaction.public()` 增加 `kind`。
- 前端：
  - `kind == "plan"` 时显示「批准计划」与「继续规划」。
  - 批准：`{"behavior":"allow"}`。
  - 继续规划：`{"behavior":"deny","reason":"The user wants to keep planning."}`。

## 8. AskUserQuestion

### 8.1 托管进程注册 hook

启动参数增加内联 `--settings`：

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "AskUserQuestion",
      "hooks": [{
        "type": "command",
        "command": "<python> <monitor.py> hook --port <port> --token <token>",
        "timeout": 604800
      }]
    }]
  }
}
```

- `python` 使用 `sys.executable`，脚本使用 `Path(__file__).resolve()`。
- Windows 用 `subprocess.list2cmdline` 引用，其他平台用 `shlex.join`。
- 若 `--claude-args` 已包含 `--settings`：不注册 hook，记录警告，AskUserQuestion 回落到 CLI 自身行为。
- `Monitor.http_port` 在 `main` 创建 HTTP server 后设置；`Monitor.hook_token` 每次启动用 `secrets.token_hex(16)` 生成。
- `http_port` 为 0/None（单元测试）时不注册 hook。

### 8.2 hook 子命令

```
python monitor.py hook --port <port> --token <token> [--event PreToolUse]
```

- 从 stdin 读取完整 JSON。
- POST 到 `http://127.0.0.1:<port>/hook/PreToolUse`，头：

```
Content-Type: application/json
X-Monitor-Token: <token>
```

- 成功后把响应体原样写 stdout。
- 连接失败、非 2xx、超时：写 `{}` 并退出 0（fail-open）。
- 超时时间：`permission_timeout + 30` 秒；permission_timeout 为 0 时使用 3600 秒。

### 8.3 服务端 `/hook/PreToolUse`

- 仅接受来自 `127.0.0.1`、`::1`、`::ffff:127.0.0.1` 的请求，否则 403。
- 校验 `X-Monitor-Token`，不匹配返回 401。
- 读取 payload：

```json
{
  "session_id": "...",
  "tool_use_id": "...",
  "hook_event_name": "PreToolUse",
  "tool_name": "AskUserQuestion",
  "tool_input": {"questions": [...]},
  "cwd": "..."
}
```

`questions` 元素形状（与 Claude 的 AskUserQuestion 一致）：

```json
{
  "question": "选择哪个方案？",
  "header": "方案",
  "multiSelect": false,
  "options": [
    {"label": "A", "description": "..."},
    {"label": "B", "description": "..."}
  ]
}
```

- 非 `AskUserQuestion`：返回 `{}`。
- 创建 `kind="choice"` 的 `Interaction`：
  - `hosted_id` 通过 `hosted_by_sid[session_id]` 查找，可能为 None；
  - `questions` 保存原 questions；
  - 加入 `mon.interactions`；
  - emit `interaction_pending`（如果会话已发现）。
- 去重键：`session_id + "\x00" + tool_use_id`。重复请求复用已有 pending 交互，两个 hook 请求等待同一个答案。
- 阻塞等待：
  - 用户提交：返回 allow + answers；
  - 跳过：返回 deny；
  - 超时：返回 deny，state=`timed_out`，reason=`审批超时，已自动跳过`。
- allow 响应：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "",
    "updatedInput": {
      "questions": [...原 questions...],
      "answers": {"<question>": "<label 或自由文本>"}
    }
  }
}
```

- deny 响应：

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "User skipped the question"
  }
}
```

### 8.4 Interaction 数据模型

`Interaction` 扩展：

- `kind`: `permission | plan | choice`
- `questions`: choice 时的问题列表
- `answers`: 用户提交的答案
- `public()` 增加 `kind` 和 `questions`。

`POST /interactions/<id>/respond` 扩展：

```json
{"behavior":"allow","answers":{"问题原文":"答案"}}
{"behavior":"deny","reason":"..."}
```

- choice 的 allow 必须有非空 `answers`。
- 每个问题必须能在 answers 中找到（多选以 `", "` 连接）。
- 未命中的问题返回 400。

### 8.5 前端选择卡片

- `kind == "choice"` 时渲染：
  - 每个问题：标题、`header`（如有）、选项；
  - 单选：radio；多选：checkbox；
  - 选项外加一个「其他」文本输入；
  - 底部「提交」与「跳过」。
- 提交时收集 `{question: answer}`；多选题把选中项用 `", "` 连接。
- 某题填写了自由文本时以文本为准，否则使用选项；文本和选项都为空视为未回答。
- 提交按钮在至少每个问题都有答案时启用。
- 跳过发送 `{"behavior":"deny"}`。
- 卡片与 approval 卡片共用 `#interactions` 容器和事件委托。

## 9. 错误处理

- SSE 连接建立后快照失败：记录错误，前端继续轮询兜底。
- 分页参数非法：400。
- `from` 超过 `total`：返回空数组，`start=end=total`。
- 文件截断：客户端 `size` 或 `total` 回退时全量重载。
- 流式文本在回合结束时必须清空；进程退出时清空。
- hook 请求非 loopback / 令牌错误：拒绝，不创建交互。
- hook 客户端失败：fail-open 输出 `{}`。
- 重复 hook 请求（相同 `tool_use_id`）：复用已有 pending，不重复创建。

## 10. 测试

### 后端

- SSE 快照：全局与单会话字段。
- `emit` transient 不写 recent、不触发 webhook。
- 分页：默认 limit、before、from、has_more、非法参数、from 超界。
- 流式：delta 聚合、节流、回合结束清空、进程退出清空。
- plan kind：ExitPlanMode 归一化。
- hook：
  - 非 loopback 403；令牌错误 401；
  - 非 AskUserQuestion 返回 `{}`；
  - allow 返回 updatedInput.answers；
  - deny 返回 permissionDecision=deny；
  - 超时返回 deny；
  - 重复 tool_use_id 不重复创建。
- Interaction choice 校验：缺少 answers、问题未覆盖。

### 前端

- `turn_delta` 更新气泡、回合结束清除。
- 分页首次加载、加载更早、增量追加、has_more 隐藏。
- 渲染缓存不重写相同 HTML。
- plan 卡片按钮与 reason。
- choice 卡片单选/多选/自由文本/跳过。
- 全局待审批计数。

### 真实 CLI 冒烟

- 不设为默认测试；新增可选检查：
  - 托管进程带 `--settings` 能正常启动并完成一轮；
  - AskUserQuestion 能触发 PreToolUse hook 并在网页回答。
- 受上游模型服务可用性影响，失败时脚本给出明确原因。

## 11. 风险

- Windows hook 命令引用错误会导致 AskUserQuestion 无法回调；实现后用 `--settings` + 真实 CLI 做启动冒烟。
- `--settings` 覆盖用户已有 settings 的 hooks；检测到已有 `--settings` 时禁用 hook 注册。
- SSE 高频事件可能压满慢客户端队列；transient 事件丢弃，正式状态由刷新修复。
- 分页只限制传输/渲染，首次仍解析整个文件；真正的尾部窗口化留到后续。
- hook 子进程每次启动 Python，首次响应有约 100ms 开销。
- 上游模型服务 503 时，真实冒烟无法完成权限/问答链路；这是环境问题，不是协议问题。

## 12. 验收标准

1. 打开会话时只传输最近 100 条消息，可加载更早，滚动位置保持。
2. 新消息通过 SSE/节流刷新在 1 秒内出现在对话中。
3. 托管回合的文本以临时气泡实时显示，结束后被正式消息替换。
4. `ExitPlanMode` 显示计划卡片，两个按钮行为正确。
5. `AskUserQuestion` 在网页显示选择卡片，回答后 CLI 不再要求 TUI 输入。
6. hook 接口拒绝非 loopback 和错误令牌。
7. 现有 85 个 Python 测试与 23 个前端测试继续通过，并新增的测试全部通过。