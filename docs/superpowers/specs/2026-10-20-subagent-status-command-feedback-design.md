# 子代理运行状态标识与 /command 即时反馈设计

日期：2026-10-20
状态：已确认（用户选择：展示位置 1A、命令反馈全选、不做子代理跳转）

## 目标

1. 看板能定位**哪些 subagent（子任务）正在运行**：折叠区与列表卡片都显示总数与
   运行中数量，折叠区内逐条标识运行状态。
2. 网页发送 `/command` 后有即时反馈：本地回显、命令气泡美化、执行中提示，
   不再"输入完没有任何显示，只有执行完才有显示"。

参考了 `_tmp/usher` 的子代理展示思路（状态圆点 + 父会话下的子代理列表），但不做
usher 式侧栏嵌套列表——本项目的会话列表是项目分组卡片，且 Agent 调用与
`agent-*.jsonl` 转录文件之间没有可靠的 id 映射，跳转子代理详情先不做。

## 一、子代理运行状态

### 运行中的判定

Agent 工具调用的 `tool_use` 出现 → 该子任务开始；对应的 `tool_result` 回传 → 结束。
这与现有 `active_agents` 集合的语义完全一致，只需把条目和集合关联起来。

### 后端（`monitor.py`）

- `Session.subagents` 条目从 `{type, description, ts}` 扩展为
  `{type, description, ts, tid, state}`：
  - `tid`：Agent 工具调用的 tool_use id；
  - `state`：`running` / `done` / `error`（tool_result 带 `is_error` 时为 error）。
- `_on_tool_use` 创建条目时写入 `tid` 和 `state: "running"`；
  `_on_tool_result` 按 `tool_use_id` 回填 `state`（条目按 tid 查找）。
  回放旧转录（首次扫描/缓存回退）走同一路径，结束时状态自然正确。
- 扫描缓存兼容：旧缓存条目没有 `tid`/`state`，`restore_cache` 时视为 `done`
  （补 `state: "done"`）；`active_agents` 里残留的 id 若找不到对应条目，维持现状
  只影响 working 判定，不影响展示。
- `summary()` 增加 `subagents_running`（state 为 running 的条数）；
  `detail()` 的 `subagents` 条目带 `state`，排序改为运行中在前、其余按 ts 倒序。
- 新增 SSE 事件 `subagent_done`（data: `{tid, state, description, type}`），
  `_on_tool_result` 中子任务结束时发出；`subagent_start` 的 data 也带上
  `tid`/`state`。

### 前端（`templates/index.html`）

- 会话卡片 chip：`${n} 子代理` → 有运行中时显示 `${n} 子代理 · ${k} 运行中`
  （运行数为 0 时保持原样，不加噪音）。
- 对话视图折叠区：
  - summary：`子代理 (3)` → `子代理 (3) · 2 运行中`（有运行中才追加）；
  - 条目渲染：运行中前置黄色脉动圆点 + “运行中”标签，done 灰点，error 红点；
  - 服务端已排序（运行中在前），前端直接按返回顺序渲染。
- SSE `subagent_start`/`subagent_done` 触发现有节流刷新即可（两者均已列入
  事件驱动刷新路径，无需新增连接）。

### 不做

- 子代理条目点击跳转到子代理转录详情（映射不可靠，后续单独立项）。
- usher 式侧栏嵌套子代理列表。

## 二、/command 即时反馈

### 问题根源

1. 发送后输入框清空，转录里的 `<command-name>...` user 行要等下一轮扫描 +
   文件 size 变化重渲染后才出现；
2. 该行被 `classify_user_line` 归为 `system`，按 500 字符截断展示原始 XML
   样板（`<command-name>/model</command-name>\n<command-message>...`），
   即使出现了也不可读；
3. 命令执行期间没有"正在执行"的提示（命令不产生 text_delta，live bubble 不动）。

### 方案（三层）

**1. 本地回显（普通消息和命令都受益）**

- 发送（`bindSendBox` / `bindNewBox` 的 onclick）成功拿到 202 后，把刚发的
  文本记入模块级 `pendingEcho = {sid: {text, ts}}`，在对话顶部渲染一条半透明
  用户气泡（复用 `msg user` 样式加 `pending` 修饰类，头部显示"发送中/排队中"）。
- 渲染时机：挂在现有 `renderConversation` 流程里——对话渲染时若存在 pendingEcho
  且转录里尚未出现该文本（比对最近一条 user 消息文本），在顶部插入；
  转录出现同文本的 user 消息后自动不再插入。
- 失败/超时：气泡变为错误样式并保留文本（现有 `sendJobs[key].status === "failed"`
  状态驱动），用户刷新或文本出现后消失。
- 新建会话面板：拿到 sid 切换到对话视图后同样生效。

**2. 命令气泡美化（后端解析 + 前端渲染）**

- 后端 `parse_msg_line`：`classify_user_line` 归为 `system` 且文本以
  `<command-name>` 开头的行，解析出 `command`（命令名，含 `/`）、`args`
  （`<command-args>` 内容，截 200 字符），生成新 part 类型
  `{type: "command", command, args}`；剩余样板文本丢弃。
  `<local-command-stdout>` 行生成
  `{type: "command_output", text}`（保留现有 500→2000 字符截断）。
- 前端 `convHtml`/`toolPartHtml`：
  - `command` part 渲染为紧凑命令气泡：`⌘ /model deepseek-v4.1-flash`，
    样式复用 `.tool` 卡片风格；
  - `command_output` 渲染为命令气泡下的折叠结果（`<details>`，默认收起，
    key 记入 `detailsOpen` 机制）。
- `SYSTEM_MARKERS` 里的 `<command-name>`/`<command-message>`/
  `<local-command-stdout>` 分类逻辑保持不变（仍非 user，不计 `user_msgs`、
  不推 `last_input_ts`）。

**3. 执行中提示**

- 发送文本以 `/` 开头且目标为托管会话时，`sendJobText` 状态文案区分：
  `queued` → `命令排队中…`，`running` → `正在执行命令 ${command}…`。
- 回合结束（`turn_finished`/`hosted_turn_done`/job 终态）由现有
  `updateSendSt` 逻辑自然清除为"已完成"。

## 测试

### 后端（`tests/test_*.py`，unittest）

- 子任务状态流转：tool_use(Agent) → 条目 running；tool_result → done；
  is_error → error；summary 的 `subagents_running` 计数；detail 排序
  （running 在前）。
- 缓存兼容：无 tid/state 的旧 `subagents` 条目 restore 后为 done。
- 命令解析：`<command-name>/model</command-name>...<command-args>x</command-args>`
  → command part；`<local-command-stdout>` → command_output part；
  普通系统文本不受影响。

### 前端（`tests/test_frontend_controls.js`，node --test）

- 命令气泡渲染：command/command_output part 的 HTML 输出。
- 卡片/折叠区文案：有/无运行中数量时的 chip 与 summary 文案。
- 本地回显：pendingEcho 存在且转录未出现同文本时插入、出现后消失
  （纯函数化 `echoVisible(messages, echo)` 便于测试）。

## 风险与边界

- 转录回放（服务重启后缓存失效全量解析）会把 running 条目正确翻成 done，
  但重启**瞬间**正在跑的子任务在回放完成前显示为 done 之外的旧值——
  实际上回放会完整重放 tool_use/tool_result，所以最终状态正确；只有
  "回放进行中"的短暂窗口内计数可能偏低，可接受。
- `<command-args>` 可能含敏感路径：与现有 `<local-command-stdout>` 展示
  策略一致（截断展示），不额外脱敏。
- 本地回显按"文本相同"对账，连续发送两条相同文本时，第一条出现即视为
  两条都已落盘——按 ts 只保留最近一条 echo，风险窗口极小，可接受。
