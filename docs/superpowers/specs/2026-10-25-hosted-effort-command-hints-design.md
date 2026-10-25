# 托管会话 effort 在线切换与斜杠指令提示设计

日期：2026-10-25
状态：已确认（用户选择：effort 方案二——即时切换接口 + 持久化；指令只做提示面板，发送不校验）

## 目标

1. 托管会话支持在线切换思考档位（effort）：发送区下拉即切，立即对常驻进程
   生效；档位按会话持久化，重托管 spawn 时带 `--effort`，rehost 后不丢。
2. 发送框输入 `/` 开头内容时，输入框下浮出指令提示面板（来自常驻进程
   `system/init` 广播的 `slash_commands`）；**发送时不校验**，照常当 user
   消息发给进程。

参考 `_tmp/usher` 的实现：在线切换用 control_request
`apply_flag_settings` + `settings: {effortLevel: ...}`（`default` 传 null
回落模型默认）；spawn 带 `--effort <档>`；指令目录来自 init 事件。不做
usher 的按模型 catalog 列 thinking_levels——固定档位 low / medium / high。

## 一、effort 在线切换

### 偏好持久化（`session_preferences.py`）

- 新增 `EffortPreferences`，结构照抄 `ApprovalPreferences`，存
  `CLAUDE_DIR/claude-sessions/efforts.json`：
  - 落盘值只允许 `low` / `medium` / `high`；清除即从 dict 删键（无 default 值）。
  - `get(sid)` 返回档位或 None（None = 默认）。

### 参数拼装（`session_preferences.py`）

- 新增 `effort_args(args, effort)`，与 `model_args` 同模式：有偏好时剥掉
  用户 `--claude-args` 里自带的 `--effort` 再追加 `--effort=<档>`；无偏好时
  原样返回（不碰用户的参数）。

### Hosted（`monitor.py`）

- `__init__` 读偏好：`self.effort = mon.efforts.get(resume_id)`（新建时由
  `/hosted` 请求传入）；`self.process_effort` 记录常驻进程当前档位。
- `start()`：`cmd += effort_args(spawn_args, self.effort)`，spawn 后
  `process_effort = self.effort`。
- 新增 `set_effort(level)`：
  - `level` 校验 `low|medium|high|default`，`default` 表示清除偏好；
  - 先持久化（`default` 删键）；
  - 进程活着且已 initialize 时 `_control("apply_flag_settings",
    settings={"effortLevel": level if level != "default" else None})`，
    成功后更新 `process_effort`；
  - `_control` 失败（CLI 不支持该控制子类型等）时偏好已保存，返回错误
    信息给前端提示"已保存，重托管后生效"；
  - 进程不在（未托管/已退出）时只保存，等下次托管 spawn 生效。

### Monitor（`monitor.py`）

- `Monitor.efforts = EffortPreferences(CLAUDE_DIR / "claude-sessions" / "efforts.json")`。
- `set_effort(sid, level)`：托管中委托 `Hosted.set_effort`；否则只存偏好。
- `host_session(...)` 增加 `effort` 参数（可选）：传入则校验并作为该托管
  进程的初始档位；拿到真实 sid 后与 model 一样 `bind`（不覆盖随后手动
  保存的选择）。

### HTTP

- `POST /sessions/<sid>/effort`，body `{"effort": "low|medium|high|default"}`：
  - 非法值 400；与审批接口一致，不校验会话存在性；
  - 成功 200 `{"effort": <生效档位或 "default">}`。
- `/hosted` body 增加 `effort`（可选）。
- `/sessions/<sid>` detail 增加 `effort`（保存的档位，None 表示默认）。

### 前端（`templates/index.html`）

- 发送区审批下拉旁新增"思考"下拉：默认 / low / medium / high。
  - 已有会话：onchange 即 `POST /sessions/<sid>/effort`；非托管会话改动
    只保存，title 提示"下次托管生效"；失败时回显原值并提示。
  - 新建会话：随首轮 `/hosted` body 提交 `effort`；`hostcb` 未勾选
    （非托管一次性消息）时该下拉禁用——effort 只作用于托管。
  - 自动刷新不重建已绑定的下拉（与审批下拉同款处理），值以服务端
    `effort` 字段为准做软同步。

## 二、斜杠指令提示

### 后端（`monitor.py`）

- `Hosted._on_init` 除 session_id 外捕获 `obj.get("slash_commands")`
  （字符串数组，含内置/插件/自定义命令），去 `/` 前缀、去重、排序后存
  `self.commands`（仅名字，init 不带描述）。
- `/sessions/<sid>` detail 增加 `commands`：托管进程活着时为目录数组，
  否则空数组。前端随现有 detail 轮询拿到，无需新端点。

### 前端（`templates/index.html`）

- 提示面板挂在 msgbox 下（复用队列摘要的浮出样式风格）：
  - 触发条件：输入框整体匹配 `^\/[^\s]*$`（单 token、`/` 在行首——
    带空格的多 token 输入是正文不是指令，不提示）且当前面板有命令目录
    （仅托管会话有）。
  - 过滤：`/` + 名字前缀匹配、区分大小写（CLI 按名字精确解析，大小写
    不符会当正文发出）。
  - 键盘：↑↓ 移动选中，Tab / 点击补全（整体替换为 `/名字 `，光标在末尾
    留空格），Esc 关闭；**Enter 保持现有行为直接发送，不拦截不校验**。
  - 无匹配 / 目录为空 / 失焦时隐藏；面板数据取最近一次 detail 的
    `commands`，随轮询自然更新。
- 新建会话页没有进程、无目录，不显示面板。
- 发送路径完全不改：`/xxx` 照常当普通文本 POST。

## 测试

### 后端（`tests/test_*.py`，unittest）

- `EffortPreferences`：合法值读写、default 清除、坏文件报错不加载。
- `effort_args`：有偏好剥用户 `--effort` 再追加；无偏好原样返回。
- `Hosted.start`：有偏好时 cmd 含 `--effort`；无偏好不含。
- `Hosted.set_effort`：进程活着时调用 `_control("apply_flag_settings")`
  且 `default` 传 `effortLevel: None`（mock 验证）；进程不在只存偏好；
  `_control` 抛错时偏好仍已保存。
- `_on_init`：slash_commands 去前缀/去重/排序；detail 的 `commands`
    托管活着返回目录、否则空。
- HTTP：`POST /sessions/<sid>/effort` 合法/非法值、404。

### 前端（`tests/test_frontend_controls.js`，node --test）

- 提示过滤与键盘逻辑纯函数化：token 提取、前缀过滤、↑↓/Tab 的选中
  与补全结果。
- 面板显示条件：单 token、有目录、有匹配。

## 风险与边界

- `apply_flag_settings` 是 usher 验证过的控制子类型；若本机 CLI 版本
  不支持，`_control` 返回错误——偏好已保存，接口把错误带给前端提示
  "重托管后生效"，不做回滚。
- 在线切换与发送/中断并发：`_control` 走独立 request_id + write_lock，
  与 `interrupt` 同款并发安全模式。
- 指令目录包含 `/clear` 等本地命令：不屏蔽、不校验（用户明确要求就当
  消息发送），CLI 自行处理；面板只是提示。
- Enter 直发可能送出未补全的半截命令，CLI 会当普通文本——预期行为。
- 新建会话未勾选托管时 effort 下拉禁用，避免"选了但一次性消息不带
  effort"的误导。
