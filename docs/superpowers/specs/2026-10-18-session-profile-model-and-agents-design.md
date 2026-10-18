# 设计：会话模型选项与子代理状态

日期：2026-10-18
状态：已批准

## 背景

看板会话详情中的模型选择目前硬编码为 `opus`、`sonnet`、`haiku` 和自定义模型 ID；会话列表与对话页均显示 cc-switch local 配置控件。子代理记录按启动顺序显示，且主会话后续的 `end_turn` 可能将仍有子代理运行的会话标为 idle。会话消息输入框目前只能点击按钮发送。

## 目标

1. 对话页移除项目 local 配置控件，local 配置操作只保留在会话列表。
2. 从 cc-switch profile 中读取名称和模型 ID；模型选项展示 profile 名称，选择后仅将其模型 ID 作为该会话的 `--model`，不应用或写入项目 local 配置。
3. 子代理列表最新的排前面；未完成子代理使会话保持 working。
4. 在会话对话页支持 Enter 发送、Shift+Enter 换行，并兼容输入法组合输入。

## 方案

### Profile 模型选项

在现有 cc-switch 服务端适配层读取 `~/.cc-switch/profiles/*.json`。每个可用 profile 只向前端返回模型选项所需字段，不返回完整 JSON、凭据、endpoint 或其他配置内容：

- 显示名：profile JSON 的 `name` 字段；缺失时用文件名（不含 `.json`）。
- 模型 ID：优先取 `env.ANTHROPIC_MODEL`；该字段不存在时回退到顶层 `model`。
- profile 结构无效、缺少可用模型 ID，或模型 ID 不符合现有模型校验规则时，不加入模型选项。
- profile 文件名排序后返回；若多个 profile 映射到同一模型 ID，保留各自名称作为选项，恢复已保存模型时匹配该 ID 的首个选项。

对话页模型下拉框使用 profile 显示名作为标签、模型 ID 作为值，同时保留“默认”和“自定义模型 ID”。会话偏好仍按现有方式保存模型 ID；消息发送、一次性任务和托管会话继续使用现有 `model` 参数路径。profile 选择不会调用 cc-switch 的 `local` 命令，不改变 `settings.local.json`。项目列表中的 local 配置控件与行为不变。

如果 cc-switch 未安装、profile 目录不可用或没有可用模型选项，页面仍可使用默认和自定义模型 ID；状态提示说明无法加载 profile 选项。配置读取错误不得把 profile 正文或异常细节返回浏览器。

### 子代理顺序与会话状态

`Agent` 工具调用记录 tool-use ID，并登记为活动子代理；遇到 `tool_result` 且 `tool_use_id` 匹配时，将其从活动集合移除。转录回放和增量解析使用同一生命周期规则。会话有活动子代理时，即使主会话最后一条 assistant 记录是 `end_turn`，摘要状态仍为 working；超过既有 stale 阈值仍显示 stale，避免无后续转录的遗留记录永久显示运行中。

子代理历史在详情数据/展示中按启动时间降序排列，最新启动项在前，较早项在后；不改变子代理记录字段及其他详情内容。

### Enter 发送

仅会话对话页的消息框增加键盘处理：Enter 触发与发送按钮相同的发送流程，Shift+Enter 保留 textarea 换行；输入法 composition 期间不触发发送。空消息、模型校验、任务状态与错误提示沿用现有发送逻辑。新建会话输入框不改变。

## 错误处理与兼容性

- 无效或不可读的 profile 不影响其他 profile 和手动模型输入。
- API 仅返回 profile 显示名和经过校验的模型 ID；不暴露 profile JSON 正文或凭据。
- profile 选项按 ID 保存，兼容已有 `opus`、`sonnet`、`haiku` 和自定义模型偏好；没有匹配 profile 的已保存 ID 仍按自定义模型显示。
- Agent tool result 缺失时活动子代理会维持到 stale 超时；stale 计算和既有 working/idle 规则以外的行为不变。

## 测试

- `cc_switch.py` 单元测试：字段优先级、名称回退、无效/缺失字段过滤、多个 profile 独立处理，以及返回值不包含凭据或 profile 正文。
- 模型执行/API 测试：profile 选项提交后偏好保存和消息执行使用模型 ID，而非显示名；不调用 local 配置应用逻辑。
- 前端测试：详情页没有 local 配置控件、列表页仍有；profile 标签和值正确；默认/自定义选项兼容；Enter、Shift+Enter 与 IME composition 行为正确。
- 会话解析测试：Agent 调用后状态为 working，匹配 tool result 后按主会话结束状态回到 idle；无匹配结果时仍 working，超过 stale 阈值变 stale；子代理列表按时间降序。
- 运行项目现有测试套件。

## 非目标

- 不在对话页应用、写入或切换 cc-switch local profile。
- 不把 profile 的完整配置、密钥或 endpoint 发送到前端。
- 不重构一次性发送、托管进程或会话偏好存储机制。
- 不改变新建会话输入框的发送键行为。

## 参考

- [ciknight/cc-switch README](https://github.com/ciknight/cc-switch)：profile 位于 `~/.cc-switch/profiles/`；示例包含顶层 `model` 与 `env.ANTHROPIC_MODEL`，本设计按用户确认以 `env.ANTHROPIC_MODEL` 优先。
