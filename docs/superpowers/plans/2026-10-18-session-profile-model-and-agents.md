# 会话模型选项与子代理状态实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将 cc-switch profile 模型选项、安全地映射到会话 `--model`；从会话对话页移除 local 配置；修正子代理排序与工作状态，并支持 Enter 发送。

**Architecture:** 在现有 `CCSwitch.status()` 服务端解析 profile JSON，只返回经过校验的名称和模型 ID；前端以模型 ID 保存偏好、以 profile 名称显示选项，并仅在会话列表保留 local 配置 UI。`Session` 跟踪 Agent tool-use ID 到 tool result 的完成关系，详情对子代理倒序；对话页 textarea 通过现有发送按钮复用发送流程。

**Tech Stack:** Python 3 标准库、HTML/JavaScript、`unittest`、Node.js `node:test`、uv。

---

## 文件与职责

- `cc_switch.py`：profile JSON 解析和安全模型选项输出；维持既有 local profile 操作路径。
- `monitor.py`：Agent 活动生命周期跟踪、working 状态合成和子代理倒序详情。
- `templates/index.html`：列表/对话页 local 控件范围、模型选项加载与呈现、会话消息框 Enter 行为。
- `tests/test_cc_switch.py`：profile 字段优先级、过滤、名称回退与敏感信息不外泄。
- `tests/test_model_execution.py`：模型 ID 经现有偏好与发送路径生效。
- `tests/test_session_state.py`（新建）：Agent 活动和会话状态/子代理排序测试。
- `tests/test_frontend_controls.js`：模型选项、local UI 隔离和键盘行为测试。
- `README.md`：更新模型选择、local 配置 UI 和 Agent 状态说明。

## 实施任务

### Task 1：为 profile 模型选项建立服务端测试

**Files:**
- Modify: `tests/test_cc_switch.py`
- Modify: `cc_switch.py`

- [x] **Step 1：添加 profile 字段优先级测试**

在 `CCSwitchTests` 增加测试，创建两个 profile 文件：`alias.json` 内容为 `{"name":"工作模型","model":"sonnet","env":{"ANTHROPIC_MODEL":"vendor-model"},"api_key":"secret"}`；`fallback.json` 内容为 `{"model":"opus"}`。调用 `status()` 并断言新返回的 `model_options` 为 `[{"name":"工作模型","model":"vendor-model"},{"name":"fallback","model":"opus"}]`，同时断言 JSON 序列化结果不含 `secret`、`api_key` 字段及配置正文。以 `patch("cc_switch.shutil.which", return_value="cc-switch")` 使 profile 列表可用。

- [x] **Step 2：运行测试确认失败**

运行：`uv run python -m unittest discover -s tests -p test_cc_switch.py -v`
预期：FAIL，当前 `status()` 不返回 `model_options`。

- [x] **Step 3：添加 profile 异常和字段校验测试**

另加测试，创建 `invalid.json`（无模型字段）、`bad-env.json`（`env.ANTHROPIC_MODEL` 为非字符串，顶层 `model` 为有效字符串）、`empty-env.json`（无 `env.ANTHROPIC_MODEL`，顶层 `model` 为 `haiku`），再断言只有 `empty-env` 可用。该断言规定：主字段存在但无效时不回退到顶层字段；主字段缺失时才回退。

- [x] **Step 4：运行新测试确认失败**

运行：`uv run python -m unittest discover -s tests -p test_cc_switch.py -v`
预期：新增断言因 `model_options` 缺失而失败；原有 cc-switch 测试保持通过。

- [x] **Step 5：实现最小 profile 解析**

在 `CCSwitch.status()` 中对现有过滤后的 profile 文件逐个读取 JSON。只接受普通非符号链接文件和 JSON object；`name` 为非空字符串时作为显示名，否则用 `Path.stem`；如果 `env` 是 object 且含 `ANTHROPIC_MODEL`，该值为主要模型 ID，否则使用顶层 `model`。主要字段存在但类型/内容无效时跳过该 profile；模型 ID 通过 `session_preferences.validate_model()` 校验，异常时只跳过该文件。返回 `model_options: [{"name": ..., "model": ...}]`，不要将解析后的配置对象或错误文本带入结果。保持现有 `profiles`、`current_profile` 和 local apply API 返回契约不变。

- [x] **Step 6：运行 cc-switch 测试**

运行：`uv run python -m unittest discover -s tests -p test_cc_switch.py -v`
预期：PASS，所有既有和新增测试通过，敏感字符串不出现在响应中。

### Task 2：用 profile 名称展示、模型 ID 发送

**Files:**
- Modify: `templates/index.html`
- Modify: `tests/test_frontend_controls.js`
- Modify: `tests/test_model_execution.py`

- [x] **Step 1：为 UI 控件增加动态 options 测试**

在 `tests/test_frontend_controls.js` 增加 Node test，设置 `curPanel={type:"conv",key:"sid"}`，调用 `modelControlsHtml()` 并检查固定 `opus`、`sonnet`、`haiku` 选项不再硬编码，且仍包含默认和自定义输入。再为 helper 测试 profile `[{name:"团队 Sonnet",model:"vendor-sonnet"}]` 渲染为文本标签 `团队 Sonnet`、option value `vendor-sonnet`（通过 fake DOM `textContent`/`value` 验证，不依赖外部 HTML 字符串注入）。

- [x] **Step 2：运行前端测试确认失败**

运行：`node --test tests/test_frontend_controls.js`
预期：新增测试因模型控件仍硬编码模型名、没有动态选项 helper 而失败。

- [x] **Step 3：让控件使用 profile 映射选项**

修改模型控件生成和绑定逻辑：保留默认、自定义 ID 选项；传入 profile model options；以 `option.textContent = profile.name` 和 `option.value = profile.model` 安全构建选项。已保存值若与 profile 模型 ID 匹配，显示匹配项；否则沿用自定义 ID 分支。重复模型 ID 恢复时使用服务端稳定排序的首个 profile 标签。模型偏好保存仍调用既有 `/sessions/<id>/model`，body 中只发送 ID/null。

- [x] **Step 4：异步加载模型选项而不触碰 local 操作**

对话详情创建时使用会话 `cwd` 请求现有 `GET /cc-switch?cwd=...`，取 `model_options` 后只刷新模型控件，不重建消息框、不覆盖当前模型草稿。cc-switch 不可用、HTTP 错误或字段缺失时保持默认和自定义项，状态文案说明 profile 选项未加载。请求只读取 status，不调用 `/cc-switch/local`。详情自动刷新保持当前未失焦草稿和输入内容。

- [x] **Step 5：验证发送路径接收实际模型 ID**

在 `tests/test_model_execution.py` 增加一次性消息测试，沿用 `ExecutionTests` 的 fake `subprocess.run` 和 `wait_done()`：调用 `send_message("existing", "hello", model="vendor-model-7")`，断言响应为 202、任务状态为 done、执行命令最后参数为 `--model=vendor-model-7`，并断言偏好记录为 `vendor-model-7`。在 `tests/test_frontend_controls.js` 再断言选择 label 为 `团队 Sonnet` 的 profile 后，请求 `/sessions/sid/model` 的 JSON body 是 `{model:"vendor-sonnet"}`，不包含 `name` 字段。

- [x] **Step 6：运行模型与前端测试**

运行：`uv run python -m unittest discover -s tests -p test_model_execution.py -v` 和 `node --test tests/test_frontend_controls.js`
预期：PASS；profile 显示名只用于 UI，既有命令/偏好只接收模型 ID。

### Task 3：项目 local UI 仅显示在会话列表

**Files:**
- Modify: `templates/index.html`
- Modify: `tests/test_frontend_controls.js`

- [x] **Step 1：添加面板控件范围测试**

新增前端测试，从项目列表、会话详情和新建会话渲染路径检查 local 配置 helper 的调用位置：列表模板包含 `ccHtml()`/`bindCc()`；详情页与新建页均无 local panel 且不调用 `bindCc()`；会话详情和新建会话仍调用独立的模型选项加载逻辑。断言文本“项目 local 配置 · cc-switch”仅从列表渲染出来。

- [x] **Step 2：运行测试确认失败**

运行：`node --test tests/test_frontend_controls.js`
预期：新断言失败，详情 HTML 仍插入 `ccHtml()` 并绑定 `bindCc()`。

- [x] **Step 3：从会话详情移除 local 配置 UI**

移除详情与新建会话模板中的 `${ccHtml()}` 及对应 `bindCc(...)`；保留 `renderSessionList()` 中的 `ccHtml()` 与 `bindCc(p.cwd)`，不改 `ccHtml`、`loadCc`、`applyCc` 或后端 local apply 路径。会话详情和新建会话仍由独立的 `loadModelOptions` 加载模型 options。

- [x] **Step 4：运行前端和管理 API 测试**

运行：`node --test tests/test_frontend_controls.js` 和 `uv run python -m unittest discover -s tests -p test_management_api.py -v`
预期：PASS；列表 local 控件功能未回归，详情不展示 local 配置。

### Task 4：Agent 生命周期与状态覆盖

**Files:**
- Modify: `monitor.py`
- Create: `tests/test_session_state.py`

- [x] **Step 1：新增 Agent 生命周期测试**

新测试文件使用 `unittest` 构造 `Monitor` 和 `Session`，通过 `handle_line()` 输入 assistant `Agent` tool_use（id `agent-use-1`），主 assistant `stop_reason=end_turn`，断言 session summary 状态为 `working`；随后输入带 `tool_result.tool_use_id="agent-use-1"` 的 user 记录，再断言无活动 Agent 且 summary 为 `idle`。

- [x] **Step 2：运行测试确认失败**

运行：`uv run python -m unittest discover -s tests -p test_session_state.py -v`
预期：FAIL，现有 `Session` 不跟踪活动 Agent，主 turn end 后状态为 `idle`。

- [x] **Step 3：覆盖无结果和 stale 边界**

增加测试：Agent 工具调用无匹配 tool result 时 summary 为 `working`；将 `last_event_ts` 设为 stale 阈值之前，状态变为 `stale`。另测试 tool result 使用非匹配 ID 时，不清除活动 Agent。

- [x] **Step 4：实现 Agent 活动追踪**

在 `Session` 新增活动 Agent tool-use ID 集合。`_on_tool_use()` 遇到 `Agent` 时加入 `b["id"]`；`_on_tool_result()` 在结果 `tool_use_id` 匹配时移除。`summary()` 状态合成按以下顺序：先沿用现有 `self.state`；若有活动 Agent 且尚未超过 stale 阈值则状态为 `working`；若超过 stale 阈值则仍为 `stale`。已存在转录扫描路径，因此首次回放与实时增量共用这套状态更新代码。

- [x] **Step 5：运行状态测试与现有测试**

运行：`uv run python -m unittest discover -s tests -p test_session_state.py -v`，再运行 `uv run python -m unittest discover -s tests -p test_management_api.py -v`
预期：PASS；Agent 运行状态不能被主 `end_turn` 覆盖，结束后恢复正常状态，stale 仍有效。

### Task 5：子代理历史倒序

**Files:**
- Modify: `monitor.py`
- Modify: `tests/test_session_state.py`

- [x] **Step 1：添加子代理排序测试**

在状态测试中创建两条子代理记录，时间分别为 `100` 和 `200`，调用 `Session.detail()`，断言输出顺序为 `[200, 100]`；同时断言内部历史仍按原加入顺序保存，避免改动事件追加语义。

- [x] **Step 2：运行测试确认失败**

运行：`uv run python -m unittest discover -s tests -p test_session_state.py -v`
预期：新增断言失败，`detail()` 当前返回追加顺序。

- [x] **Step 3：只在详情序列化时倒序**

在 `Session.detail()` 把 `subagents` 序列化为 `list(reversed(self.subagents))`；不改变追加、SSE、持久化或其他计数行为。

- [x] **Step 4：运行会话状态测试**

运行：`uv run python -m unittest discover -s tests -p test_session_state.py -v`
预期：PASS，详情最新子代理在前，内部记录行为不变。

### Task 6：会话消息框 Enter 发送

**Files:**
- Modify: `templates/index.html`
- Modify: `tests/test_frontend_controls.js`

- [x] **Step 1：添加键盘交互测试**

扩展 fake DOM 支持 textarea `addEventListener` 回调及键盘事件。新增测试分别模拟普通 Enter、Shift+Enter、`isComposing=true` 的 Enter：普通 Enter 调用现有 send button click 一次并 preventDefault；Shift+Enter 不发送且不 preventDefault；composition Enter 不发送。

- [x] **Step 2：运行前端测试确认失败**

运行：`node --test tests/test_frontend_controls.js`
预期：FAIL，现有 `bindSendBox()` 没有给 textarea 注册键盘处理器。

- [x] **Step 3：绑定会话消息框键盘处理**

在 `bindSendBox(id, d)` 绑定 `msgbox` 的 `keydown`。当 `event.key === "Enter"`、未按 Shift、`!event.isComposing` 且 `event.keyCode !== 229` 时调用 `preventDefault()` 并触发现有 `sendbtn.click()`（若 fake DOM 不支持 click，则将发送处理抽成单一函数并由按钮和键盘共用，避免复制发送流程）。Shift+Enter 和 composition 直接返回。仅会话页调用该绑定；`bindNewBox()` 不变。

- [x] **Step 4：运行前端测试**

运行：`node --test tests/test_frontend_controls.js`
预期：PASS；发送仍经过原流程，IME 和换行行为正确。

### Task 7：文档与全量验证

**Files:**
- Modify: `README.md`

- [x] **Step 1：更新用户文档**

更新“网页模型与项目配置”说明：会话详情从 cc-switch profile 显示名称，模型值优先为 `env.ANTHROPIC_MODEL`，缺失时为 profile 顶层 `model`；选择只对该 session 的看板消息作为 `--model` 生效，不应用 local；local 配置仅从会话列表管理。更新状态说明：未完成 Agent tool result 时 session 为 working、子代理详情最新优先，并注明对话框 Enter 发送、Shift+Enter 换行。

- [x] **Step 2：运行 Python 全量测试**

运行：`uv run python -m unittest discover -s tests -v`
预期：PASS，所有现有和新增单元测试通过。

- [x] **Step 3：运行 JavaScript 前端测试**

运行：`node --test tests/test_frontend_controls.js`
预期：PASS。

- [x] **Step 4：检查差异和工作区**

运行：`git diff --check` 与 `git status --short`。检查本计划涉及文件，无意外 `.claude` 配置修改、profile 正文/凭据泄露、无关改动；确认模型值在 POST 与命令中均为 ID，不是显示名称。

- [x] **Step 5：最终提交实现**

按用户要求，完成并通过测试后，在当前分支一次性提交代码变更（不提交中间实现）。提交信息建议：

```text
feat: 支持 profile 会话模型与子代理状态

Co-Authored-By: Claude Code <noreply@anthropic.com>
```
