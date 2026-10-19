# 托管会话审批、中断、排队与回合判定设计

日期：2026-10-19
状态：已确认，待实现

## 1. 目标

在现有 `monitor.py` 单文件服务上补齐三组能力：

1. 常驻 stream-json 托管进程的权限审批、中断、可靠排队；
2. 双时钟与显式回合结束判定，取代依赖 `stop_reason=end_turn` 和文件 mtime 的状态推断；
3. 转录解析增强：`toolUseResult`、`isMeta` 细分、子代理 jsonl、compaction / API error、多 tool_result、context 用量。

非目标：fork 会话、模型目录读取、鉴权、一次性任务的权限审批、前端框架/构建工具。

## 2. 约束

- 保持单文件服务结构：`monitor.py` + Jinja 模板，不新增运行时依赖。
- 一次性 `/sessions/<id>/message` 路径行为不变，不可中途审批或中断。
- 所有 JSON 状态文件沿用原子写（临时文件 + `os.replace`，0600）。
- 日志不得输出 prompt / 转录正文。
- 现有单元测试风格为 `unittest` + `tests/fake_claude.py`。

## 3. 托管进程协议

### 3.1 启动参数

```text
claude -p --input-format stream-json --output-format stream-json --verbose
       --permission-prompt-tool stdio
       [--resume <id> | --session-id <uuid>]
       <既有 model / claude_args>
```

- `--permission-prompt-tool stdio` 始终添加。
- 新建会话预生成 UUID 并通过 `--session-id` 传入，使 session id 在首轮前已知；现有 init 回填逻辑保留（init 返回值与预生成值不一致时以 init 为准并记录警告）。
- 审批模式 `approval`：
  - `ask`（默认）：不额外添加 `--permission-mode`，尊重 `--claude-args` 中的设置；
  - `auto`：若 `--claude-args` 未指定 `--permission-mode`，追加 `--permission-mode auto`；运行中通过 `set_permission_mode` 切换；
  - `full`：不改变启动模式，由 monitor 对 `can_use_tool` 自动回 allow。

### 3.2 发送用户消息

```json
{"type":"user","uuid":"<uuid>","message":{"role":"user","content":[{"type":"text","text":"..."}]}}
```

- `uuid` 由 monitor 生成，作为该 job 的 `command_uuid`。
- 每个 job 记录：`id`、`command_uuid`、`hosted_id`、`status`、`text`、`requested_model`、`result`、`error`、`finished_at`。

### 3.3 回合生命周期

- 主判定：stdout 的 `command_lifecycle`。
  - 非终态：`queued / started / running / released`，job 保持 running。
  - `state=completed`：job → done（若 result 标记错误则 failed）。
  - `state=cancelled`：job → cancelled。
  - `state=discarded|refused|dropped`：job → failed，error 记录状态。
- `result`：只填充 `result`、`result_session_id`、`cost_usd`、`is_error`；不单独释放队列。
- 兜底：若收到 `result` 后 3 秒内没有对应 `command_lifecycle`（旧版 CLI、测试 fake），用 `result` 结束 job。
- 未匹配当前 job 的 `command_uuid` 视为 foreign turn：仅发 `hosted_foreign_turn` 事件，不改变队列状态。

### 3.4 权限请求

请求帧：

```json
{"type":"control_request","request_id":"...","request":{
  "subtype":"can_use_tool",
  "tool_name":"Edit",
  "tool_use_id":"...",
  "input":{...},
  "permission_suggestions":[...],
  "decision_reason":"...",
  "suppress_always_allow_rule":false
}}
```

归一化 Pending：

| 字段 | 说明 |
|---|---|
| `id` | 等于 `request_id` |
| `session_id` / `hosted_id` | 所属会话与托管进程 |
| `tool_name` / `tool_input` | 原样保存 |
| `preview` | Edit/Write：`difflib.unified_diff`；Bash：完整命令；其他：JSON 截断 |
| `reason` | 去除 ANSI 转义后的 `decision_reason` |
| `allow_always` | `permission_suggestions` 中存在 `behavior=allow` 且未 `suppress_always_allow_rule` |
| `created_at` | 时间戳 |
| `state` | `pending / allowed / denied / cancelled / timed_out` |

响应帧：

- allow：

```json
{"type":"control_response","response":{"subtype":"success","request_id":"...","response":{
  "behavior":"allow",
  "updatedInput":{...原 input...},
  "updatedPermissions":[...仅 scope=always...]
}}}
```

- deny：

```json
{"type":"control_response","response":{"subtype":"success","request_id":"...","response":{
  "behavior":"deny","message":"..."
}}}
```

- 服务内部错误：`subtype=error`。
- 收到 CLI 的 `control_cancel_request` 时标记 `cancelled` 并唤醒等待者。

模式行为：

- `ask`：进入 pending，阻塞等待用户响应。
- `full`：立即 allow，仍记录并发送 `interaction_resolved` 事件。
- `auto`：由 CLI 自身权限模式处理；若仍收到请求，按 `ask` 展示。

超时：

- `--permission-timeout` 默认 300 秒；0 表示无限等待。
- 超时回 deny，`message="审批超时，已自动拒绝"`，状态 `timed_out`。
- 进程退出、托管 stop、服务退出时，所有该进程 pending 置 `cancelled` 并通知。

### 3.5 中断与队列

- 队列：每托管进程 FIFO，上限 32。
- 队列满：返回 429，job 不创建。
- 中断入口：`POST /sessions/<id>/interrupt`。
  - 清理队列中所有 job，状态 `cancelled`，error 为「已中断」。
  - 向 stdin 写：

```json
{"type":"control_request","request_id":"...","request":{"subtype":"interrupt"}}
```

  - 30 秒宽限：没有 `command_lifecycle` 终态则强制结束当前 job（`cancelled`）。
  - 进程保留，可继续接收新消息。
- 非托管会话调用中断返回 409。

### 3.6 审批模式持久化

- 新文件：`<CLAUDE_DIR>/claude-sessions/approvals.json`。

```json
{"version":1,"approvals":{"<session-id>":"ask"}}
```

- 原子写、0600、目录 0700。
- 读取损坏时回退为空并记录一次性错误，不阻塞服务。
- 托管启动时使用该会话的持久化模式；`POST /sessions/<id>/approval` 同时更新文件和活动进程。

## 4. 双时钟与状态

### 4.1 Session 字段

新增：

- `last_input_ts`：最后一次真实用户输入的 timestamp；
- `last_turn_end_ts`：最后一次显式回合结束的 timestamp；
- `saw_turn_duration`：该会话是否出现过 `system/turn_duration`；
- `pending_tools`：主链上尚未收到 tool_result 的 tool_use id 集合；
- `context_tokens`：最新一条 assistant 消息的 `input + cache_creation + cache_read + output`，覆盖不累加，0 值不清空旧值；
- `compacted_at`：最后一次 `compact_boundary` 的时间；
- `is_subagent`、`parent_id`、`agent_name`（子代理文件）。

`last_event_ts` 语义改为「最后一条带 timestamp 的转录事件的时间」，不再在后继扫描时用 `now_ts()` 覆盖；无 timestamp 时首次扫描回退 mtime，后续回退 `now_ts()`。

### 4.2 真实用户输入判定

只有同时满足以下条件才推进 `last_input_ts`、计入 `user_msgs`、设置 working：

- `type=user`；
- 不含 `tool_result`；
- 非 `isMeta`；
- 文本不以系统标记开头（`<task-notification>`、`<command-name>`、`<command-message>`、`<local-command-stdout>`、`Caveat:`、`[Request interrupted`）；
- 无 `origin.kind` / `promptSource` 注入标记。

### 4.3 回合结束

1. `type=system, subtype=turn_duration`：设置 `saw_turn_duration=True`、`last_turn_end_ts`；若 `pending_tools` 为空且无活动子代理 → idle；`turn_finished` 仅在状态从 working 变为 idle 时 emit。
2. 回退：仅当 `saw_turn_duration=False` 时，assistant `stop_reason=end_turn` 且主链无 pending tool_use → idle；同样只在 working → idle 时 emit `turn_finished`。新的真实用户输入使状态回到 working，从而允许下一回合再次 emit。
3. `sidechain` / 子代理活动期间不置 idle；`summary()` 仍按 `active_agents` 覆盖为 working。

### 4.4 stale 与排序

- stale：`now - last_event_ts > stale_after`（保留现有严格大于边界）。
- 列表排序：`last_input_ts`，缺失时回退 `last_event_ts`。
- API 暴露 `last_input_at` / `last_event_at` / `last_turn_end_at`。

## 5. 转录解析增强

### 5.1 toolUseResult

行级兄弟字段 `toolUseResult` 与 `message.content` 中的 `tool_result` 按 `tool_use_id` 关联。提取规则：

- Edit/Write：优先 `structuredPatch`，生成统一 diff 文本；缺失时用对应 `tool_use.input` 的 `old_string/new_string` 兜底。
- Bash：`stdout` / `stderr`，各截断。
- Read：文件路径与行数摘要，不内联大段文件内容。
- 图片：`source.type=base64/image` 或 `toolUseResult` 中的图片引用 → `kind=image`，保留路径/媒体类型。
- 其他：文本截断。

会话消息中的工具结果 part：

```json
{"type":"tool_result","tool_use_id":"...","name":"Edit","is_error":false,
 "kind":"diff|text|image","text":"...","tool_input":{...}}
```

错误结果保留 `is_error=true` 并继续走现有 `terror` 展示。

### 5.2 isMeta 三分

- 无 `sourceToolUseID` 且无 `origin/promptSource`：boilerplate，丢弃，不打断 assistant 回合，不计用户输入。
- 有 `sourceToolUseID`：内容并入对应 tool part（例如 Skill 注入），不新建消息。
- 有 `origin.kind` 或 `promptSource`：生成 system 回合；不计用户输入、不推进 `last_input_ts`，但不吞掉 assistant 回合。

### 5.3 子代理文件

- 扫描 `projects/*/<parent-sid>/subagents/agent-*.jsonl`。
- Session id：`<parent-sid>::agent-<文件名 hash/stem>`。
- `is_subagent=True`，`parent_id=<parent-sid>`。
- `agent_name` 优先取行内 `attributionAgent`，否则用 `agent-<hash>`。
- 默认 `/sessions` 不返回子代理；`?include_subagents=1` 返回。
- 父会话详情增加 `subagent_session_ids`。
- 子代理会话禁止发消息、托管（400）。

### 5.4 其他事件

- `system/compact_boundary`：设置 `compacted_at`，emit `context_compacted`；不立即清零 `context_tokens`。
- `isCompactSummary=True` 的 user 行：视为 system，不计用户输入。
- `isApiErrorMessage=True`：emit `api_error`，不直接改变状态，交给 `turn_duration` / 回退。
- `type=custom-title`：`customTitle` 非空则覆盖标题；空字符串表示清除并回退 `aiTitle`。
- 同一 user 行多个 `tool_result`：全部处理。

### 5.5 容错

- 损坏 JSON 行跳过，不中断文件读取。
- 空文件：先建立 Session，标题/cwd 缺失；后续写入补齐。
- 文件变小：offset 与半行 buffer 重置。
- 半行：保留到下次拼接。
- 单行过大：按现有读取方式处理，日志只记录长度，不输出内容。

## 6. API

新增：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/interactions?session=<id>` | 待审批列表 |
| POST | `/interactions/<id>/respond` | `{behavior:"allow"\|"deny", scope:"once"\|"always", reason?}` |
| POST | `/sessions/<id>/interrupt` | 中断托管当前回合并清空队列 |
| POST | `/sessions/<id>/approval` | `{approval:"ask"\|"auto"\|"full"}` |

扩展：

- `/sessions/<id>`：`interactions`（仅该会话当前 pending 的审批）、`approval`、`is_subagent`、`parent_id`、`last_input_at`、`last_turn_end_at`、`context_tokens`、`compacted_at`、`subagent_session_ids`。
- `/sessions`：`last_input_at`、`is_subagent`、`pending_interactions` 计数；`?include_subagents=1`。
- `/hosted`：`approval`、`current_job`、`queued`、`interrupting`。
- job 状态增加 `cancelled`。
- SSE 新增事件：`interaction_pending`、`interaction_resolved`、`turn_interrupted`、`context_compacted`、`api_error`、`hosted_foreign_turn`。

## 7. 前端

保持自包含 SPA 与现有轮询机制。

- 对话视图在输入区上方显示待审批卡片：
  - 工具名与目标（file_path / command / 其他摘要）；
  - `reason`；
  - diff / 命令预览（`<pre>`，错误行不执行 HTML）；
  - 按钮：允许一次；`allow_always` 时显示始终允许；拒绝。
  - 响应后卡片立即移除；失败时显示错误并保留。
- 发送区增加「中断」按钮：托管进程存在时显示；无当前 job 或队列时禁用；点击后状态显示「中断中…」。
- 托管会话增加审批模式下拉：ask / auto / full，保存到 `/sessions/<id>/approval`。
- 工具结果：
  - diff 用 `.diff-add/.diff-del/.diff-hunk` 简单着色；
  - 文本结果放入可折叠 `<pre>`；
  - 图片仅显示标识和路径，不新增图片接口。
- 渲染使用 `esc()`，不使用 `innerHTML` 拼接未转义内容。

## 8. 错误处理

- 权限响应写入失败：Pending 保留，返回 500，允许重试。
- 进程已退出：pending 置 cancelled，队列 job 置 failed。
- `auto` 模式实时切换失败：返回 409，界面回到原模式。
- 中断宽限期后进程仍未恢复：强制结束 job，不杀进程。
- 服务退出：所有宿主 stop，pending cancelled。
- 队列满：429，错误信息明确。

## 9. 测试

新增/更新测试：

1. 转录与状态
   - `turn_duration` 优先于 `end_turn`；
   - 无 `turn_duration` 时 `end_turn` 回退且只 emit 一次；
   - tool_result / isMeta / 注入 prompt 不推进 `last_input_ts`；
   - isMeta 三分；
   - 同一行多个 tool_result；
   - toolUseResult diff / stdout / 图片；
   - compact_boundary、isApiErrorMessage、custom-title；
   - 子代理路径解析与默认隐藏；
   - context_tokens 覆盖语义。
2. 托管协议
   - 扩展 `tests/fake_claude.py`：`command_lifecycle`、`can_use_tool`、slow、reject、timeout；
   - allow / deny / always；
   - full 自动放行；
   - 审批超时 deny；
   - 队列满 429；
   - 中断清空队列并发送 interrupt；
   - 进程退出清理 pending；
   - 仅 `result` 的旧版兜底。
3. API
   - interactions / approval / interrupt 端点；
   - 跨站写入拒绝；
   - 子代理禁止发消息。

## 10. 风险

- `--permission-prompt-tool` 在部分旧版 CLI 中不可用；本机 2.1.286 已确认。若无 `command_lifecycle`，result 兜底保证可用。
- 审批阻塞可能让无人值守的托管会话停在等待；默认 300 秒超时拒绝，`full` 模式可完全无人值守。
- 子代理文件会显著增加会话数量；默认隐藏，仅显式请求时返回。
- `last_event_ts` 语义变化会影响排序和 stale 判定；现有 stale 测试使用行内 timestamp，预期兼容。