# 托管会话 auto 权限模式分类器故障分析

> 日期：2026-10-20
> 状态：根因已定位；litecore 端待修复；monitor 侧暂不改动（bypass 档未加）

## TL;DR

托管会话（`approval=auto`）一到执行任务阶段，所有 Bash/PowerShell 调用被拒，
内层模型报「Shell 分类器不可用 / 模型侧临时故障」。**这不是临时故障，是确定性故障。**

根因链：monitor 给托管进程注入 `--permission-mode auto`（monitor.py:1330-1332）→
auto 模式下每次 shell 调用前 Claude Code 会**另发一条独立的安全分类器请求**，走小模型档
（`glm-5.3-flash[1m]`）→ litecore 端点对该请求的响应缺少 `usage` 字段（或返回非标准错误体）→
Claude Code 解析崩溃 `undefined is not an object (evaluating 'pr.usage.input_tokens')` →
fail-closed 拒绝工具调用。

交互式 PowerShell 会话不受影响，因为其 `settings.local.json` 配置
`permissions.defaultMode: "bypassPermissions"`，**根本不产生权限检查，分类器从未被调用**。

## 现象与误诊

内层会话（glm-5.3）的典型输出：

> Shell 分类器仍未恢复，连续三次失败。……这是模型侧的临时故障，通常几分钟内恢复。

注意：**这是内层模型的误诊**。等待无效，证据见下节——跨多个项目、多个会话、持续多日、
100% 复现。主对话（`query_source:"sdk"`，`glm-5.3[1m]`）始终正常，只有分类器请求
（`query_source:"auto_mode"`）失败，所以「大模型是好的」只对主对话成立。

被拦截时 Claude Code 的原文：

```
glm-5.3[1m] is temporarily unavailable, so auto mode cannot determine the safety of
Bash right now. Wait a moment and then try this action again. ... Note: reading files,
searching code, and other read-only operations do not require the classifier and can
still be used.
```

即：只读工具（Read/Grep/Glob）不受影响，只有 shell 类工具被 fail-closed 拦截——
这解释了「一到执行任务阶段就出问题」（任务执行 = 跑命令 = Bash）。

## 证据链

1. **错误转储**：`~/.claude/projects/<项目>/<sessionId>/auto-mode-classifier-error.txt`
   （Claude Code 在分类器失败时自动落盘）。共 6 份，横跨 clash3d / LiveGameLauncher /
   simplicai / claude-sessions 四个项目，全部是同一个崩溃：
   ```
   === ERROR ===
   undefined is not an object (evaluating 'pr.usage.input_tokens')

   === CONTEXT COMPARISON ===
   model: glm-5.3[1m]
   mainLoopTokens: 113276 / 152047
   classifierChars: 197605 / 190283
   classifierTokensEst: 49401 / 47571
   ```
   分类器每次请求带整个会话转录 + 一大段安全审查 system prompt（约 4.7~4.9 万 token，
   远未超 1M 上下文，排除长度问题）。LiveGameLauncher 那份的被拦动作正是
   `git checkout -b feat/clash3d-local-control-batch2`。

2. **monitor stderr 遥测**（hosted 进程退出时）：
   ```
   [claude-code:unrecognized_model] {"model":"glm-5.3[1m]","query_source":"sdk"}
   [claude-code:unrecognized_model] {"model":"glm-5.3-flash[1m]","query_source":"auto_mode"}
   ```
   （simplicai 会话同模式：`gpt-6.1-sol[1m]` 主对话 / `gpt-6.1-luna[1m]` auto_mode）
   证实**分类器用的不是主模型**，而是小模型档映射的 `glm-5.3-flash[1m]`。

3. **现场复现**：排查会话本身运行在 auto 模式下，第一次尝试跑 Bash/PowerShell 取证
   即被同一条规则拦截——故障至今仍在，非「几分钟内恢复」。

4. **探测脚本** `probe_usage.py`（仓库根目录）：分别探测两模型的流式/非流式响应。
   当前结果：四个组合全部 503（账号池忙/限流），错误体为 litecore 自有格式：
   ```json
   {"code":503,"message":"model temporarily unavailable: ... All accounts for this
   model are busy or rate-limited, please retry later."}
   ```
   这**不是 Anthropic 标准错误信封**（规范应为
   `{"type":"error","error":{"type":"overloaded_error","message":...}}`），
   是最可疑的崩溃诱因。flash 成功响应是否缺 `usage` 待容量恢复后复测。

## 配置事实（cc-switch 的 glm profile）

项目 `.claude/settings.local.json`（由 cc-switch `local` 写入，`~/.cc-switch/profiles/glm.json`）：

```jsonc
{
  "env": {
    "ANTHROPIC_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3-flash[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash[1m]",
    "ANTHROPIC_SMALL_FAST_MODEL": "glm-5.3-flash[1m]",
    "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5.3[1m]",
    "ANTHROPIC_BASE_URL": "https://litecore.ai/dist"
  },
  "permissions": { "defaultMode": "bypassPermissions" }
}
```

- 主对话走 `ANTHROPIC_MODEL`（glm-5.3，响应带 usage，monitor 能正常统计 token）；
- auto 分类器走 Haiku/SMALL_FAST 档 → `glm-5.3-flash[1m]` → 崩；
- 交互式默认 bypassPermissions → 无权限检查 → 无分类器 → 无故障。

## 权限模式与审批档位对照

**Claude Code 权限模式**（互斥四档，CLI 参数优先于 settings）：

| 模式 | 行为 |
|---|---|
| `default` | 敏感操作逐个询问用户（交互式出厂默认） |
| `plan` / `acceptEdits` | 限制范围的中庸档 |
| `bypassPermissions` | 全跳过，不产生任何权限请求 |
| `auto` | 不问用户，每次 shell 调用由安全分类器（小模型独立请求）自动裁决；分类器失败即 fail-closed 拒绝 |

**monitor 托管审批档**（monitor 自己的语义，`session_preferences.py:12`
写死 `("ask", "auto", "full")`，持久化于 `~/.claude/claude-sessions/approvals.json`）：

| 档位 | 实际效果 | shell 调用前发生什么 |
|---|---|---|
| `auto`（默认） | 注入 `--permission-mode auto` | 每次调分类器；分类器挂了全拦（当前故障路径） |
| `ask` | 不注入权限模式 | 权限请求经 `--permission-prompt-tool stdio` 发网页审批卡片，人工放行 |
| `full` | 服务自动放行 | 权限请求照常产生，monitor 自动点同意 |

`bypassPermissions` **不在** monitor 审批三档里，目前只能通过启动参数全局注入：
monitor.py:1330 检测到 `--claude-args` 已含 `--permission-mode` 时不再追加 auto
（`session_preferences.py:35` 的 `model_args` 会透传其余参数）。

## 修复方案

| 方案 | 做法 | 代价 |
|---|---|---|
| A. 改小模型档映射 | cc-switch glm profile 把 `ANTHROPIC_DEFAULT_HAIKU_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL`（可含 SONNET 档）指向 `glm-5.3[1m]`，重新 `cc-switch local glm` + 重新托管 | 分类器按大模型计费，稍贵稍慢；保留 auto 的安全防护 |
| B. 全局 bypass | `uv run monitor.py --claude-args "--permission-mode bypassPermissions"` | 与 PowerShell 交互体验完全一致；但**所有**托管会话裸奔，审批功能（ask/full）作废 |
| C. 会话切 ask | 网页审批下拉选 `ask`，shell 操作人工放行 | 有审批闸但要点按钮 |
| D. 修 litecore（根治） | 见下节 bug 报告 | 依赖端点侧；修好后 flash 可继续留在小模型档省 token |

## 给 litecore 的 bug 报告

**端点**：`https://litecore.ai/dist`（Anthropic `/v1/messages` 兼容）
**客户端**：Claude Code CLI，`--permission-mode auto`

### 现象

auto 模式下每次 shell 工具调用前，Claude Code 会另发一条安全分类器请求
（`query_source:"auto_mode"`，走小模型档 `glm-5.3-flash[1m]`）。该请求**每次**导致
Claude Code 内部异常：

```
undefined is not an object (evaluating 'pr.usage.input_tokens')
```

随后 fail-closed 拒绝所有 shell 调用。跨会话/项目/多日 100% 复现；
主对话请求（`glm-5.3[1m]`）始终正常。

### 疑似原因

1. **错误响应不是 Anthropic 标准格式**（最可疑）。上游账号池忙/限流时返回：
   ```json
   {"code":503,"message":"model temporarily unavailable: ..."}
   ```
   客户端分类器解析路径无条件访问 `<响应>.usage.input_tokens`，非标准体上该字段不存在
   即崩溃。「每次都炸」与账号池常态限流 + 非标准错误体高度吻合。
2. **flash 系列成功响应可能缺 `usage`**（待复测）。规范要求：非流式顶层
   `usage:{input_tokens,output_tokens}` 必须存在；流式 `message_start` 事件
   `message.usage` 含 `input_tokens`、`message_delta` 事件 `usage` 含 `output_tokens`。

### 修复要求

1. 所有错误路径返回 Anthropic 标准错误信封 + 语义正确的状态码（过载 529/503 +
   `overloaded_error`），让客户端正常重试而非解析崩溃；
2. 所有成功响应（含 SSE 各事件）保证 `usage` 对象及两个 token 键始终存在，
   含 flash 系列；上游不回传时请映射（`prompt_tokens→input_tokens`、
   `completion_tokens→output_tokens`），实在没有补 0，**字段必须存在**；
3. 容量恢复后用 `probe_usage.py` 专项复测 `glm-5.3-flash[1m]`。

## `[1m]` 后缀问题（litecore 反馈，2026-10-20 追加）

litecore 侧反馈：请求里的模型名是 `glm-5.3-flash[1m]`，`[1m]` 是 Claude 侧的标识，
不应透传给 litecore。

**为什么会发过去**：`[1m]` 不是 Claude Code 追加的，是 cc-switch 的 glm profile
自己配的（六个 env 值全带后缀）。Claude Code 的处理规则：

- 对**自家模型名**（如 `claude-opus-5-5[1m]`）：认识后缀，发请求前剥掉，
  改为基础模型名 + 1M context 的 beta 头；
- 对**自定义模型名**（`glm-5.3[1m]` 等）：`unrecognized_model` 遥测为证，
  **不解析、不剥后缀、不加 beta 头、无任何 1M 行为**，整个字符串原样塞进请求的
  `model` 字段。

**关键澄清（当时的概念混淆）**：

- `SONNET_MODEL` / `HAIKU_MODEL` 等只是 Claude Code 的**档位槽名**，映射到什么模型
  由配置决定，与 Anthropic 家族模型是否支持 1M 无关；
- 自定义模型名带 `[1m]` **不产生任何 1M 能力**：客户端的上下文记账由
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000` 控制（profile 已配），实际窗口由
  litecore 后端模型决定。去掉后缀不损失任何能力；
- 主模型 `glm-5.3[1m]` 带后缀也一直正常（litecore 错误消息里原样回显，说明能路由），
  所以 `[1m]` 是否是分类器崩溃的直接原因取决于 litecore 模型表里有没有
  `glm-5.3-flash[1m]` 这个条目——待 litecore 确认。

**已做的修改**（2026-10-20）：`~/.cc-switch/profiles/glm.json` 六个模型值全部去掉
`[1m]` → `glm-5.3` / `glm-5.3-flash`。其他一律未动。

## 未决事项

- litecore 修复后：先用 `python probe_usage.py` 确认 flash 流式/非流式 usage 齐全，
  再恢复 auto 审批 + flash 小模型档；
- profile 已去 `[1m]` 但**尚未重新应用**：待 litecore 处理完后，在项目里跑
  `cc-switch local glm` 重新生成 `.claude/settings.local.json`，退出托管重新托管，
  再观察分类器是否恢复（注意各项目的 settings.local.json 仍是带 `[1m]` 的旧值，
  重新应用前不生效）；
- 待 litecore 确认：`glm-5.3-flash[1m]` 与 `glm-5.3-flash` 在模型表里是否都存在，
  以判定 `[1m]` 是否是崩溃直接原因；
- monitor 是否加第四档 `approval: "bypass"`（按会话注入
  `--permission-mode bypassPermissions`，走 `needs_rehost` 热切换，
  前端下拉加红色警示标签）：**已讨论，暂不加**；
- 可选的 monitor 侧改进（已提出未实施）：检测到 `automode-unavailable` 结果时，
  在网页审批卡片上提示「端点不兼容 auto 分类器，建议切 ask」，避免内层模型误判为
  临时故障而干等。

## 排查过程时间线（供复现参考）

1. 内层会话报「分类器连续失败」→ 怀疑非临时故障；
2. 读 monitor.py 托管启动代码：定位 `--permission-mode auto` 注入点（1330-1332 行）；
3. Glob 发现 6 份 `auto-mode-classifier-error.txt`，全部同一崩溃，含被拦命令原文；
4. 排查会话自身跑 Bash 被拦（现场复现，证实故障持续且确定性）；
5. monitor stderr 的 `unrecognized_model` 遥测暴露分类器走 `glm-5.3-flash[1m]`；
6. 读 cc-switch 适配层与项目 settings.local.json：确认小模型档映射 + 交互式
   `bypassPermissions`，根因链闭合；
7. `probe_usage.py` 探测：当前全线 503，错误体为 litecore 自有格式——
   崩溃诱因头号嫌疑，待容量恢复复测。
