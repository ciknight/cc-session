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
- 无测试框架、无 lint 配置；直接依赖 Jinja2 与 psutil（系统面板采样），其余为标准库。
- 单元测试：`PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v`；
  前端测试：`node --test tests/test_frontend_controls.js`。
- 真实 CLI 权限冒烟（会调用模型）：`CLAUDE_SESSIONS_SMOKE=1 uv run python tests/smoke_permission_live.py`；
  AskUserQuestion 冒烟加 `SMOKE_ASK=1`。
- 可用 `CLAUDE_CONFIG_DIR` 环境变量指向其他 Claude 配置目录做测试。

## 架构

根 `monitor.py` 是入口与兼容 facade（re-export 公共符号，`import monitor` 的旧用法与
`uv run monitor.py` 均不变）；实现按职责拆在 `claude_sessions/` 包：

- `core.py` — `Monitor`：会话注册表、轮询扫描、远程消息/队列、审批协调、事件分发（协调中枢）
- `hosted.py` — `Hosted`：常驻 claude 进程（stream-json 双向）
- `session.py` — `Session`：单个转录的累计状态与列表/详情投影
- `transcript.py` — 转录行解析（`parse_msg_line` 等对话消息提取）
- `http_api.py` — `make_handler`：REST/SSE/hook 回调/静态资源
- `interactions.py` — `Interaction` 与审批预览；`system.py` — 系统采样、电源与推迟睡眠；
  `orphans.py` — 托管残留进程观察；`commands.py` — slash 命令扫描；
  `prompts.py` — 常用提示词模板的服务端 JSON 存储（原子写、mtime 自动重载、数量/长度/字符校验）；
  `fileview.py` — 文件预览的分类与文本解码（BOM/UTF-16 启发 → utf-8 → gb18030 回退）；
  `util.py` — 纯工具函数；`config.py` — 路径与可调常量；
  `hook.py` — hook 客户端；`app.py` — CLI 入口与完整用法文档

**可替换依赖约定**：`config` 常量、`system` 电源/采样函数、`util.now_ts` 时钟一律以
模块属性访问（`config.X` / `system.X` / `util.now_ts()`），测试 patch 对应模块属性即全局生效；
其余纯函数自由 from import。根级另有扫描缓存 `scan_cache.py` + 会话偏好
`session_preferences.py` + cc-switch 适配 `cc_switch.py` + Jinja 模板 `templates/index.html` +
独立文件预览页 `templates/preview.html` +
本地前端依赖 `static/marked.min.js`、`static/purify.min.js`（不依赖外网 CDN）。

### 数据流

1. **轮询线程**（`Monitor.run` → `scan_once`）：按 `--interval` 秒扫描 `CLAUDE_DIR/projects/*/*.jsonl`，
   每个 `Session` 维护文件 offset 增量读取，半行留在 buffer 下次拼接。同一路径由
   每文件扫描锁串行化，stat/读文件/缓存校验**和逐行解析**都在 `mon.lock` 外执行，
   只有注册、截断判定、offset/buffer 提交持全局锁，慢盘或大增量不阻塞 HTTP 与审批。
   单次轮询最多解析 `config.SCAN_MAX_LINES_PER_TICK` 行、读取
   `config.SCAN_MAX_BYTES_PER_TICK` 字节，超大转录首次回填时分批进行；未读取的字节
   留在文件里，未解析的整行留在 `Session.buf`（offset 仍是已读位置），文件没有新增时
   也继续消化。缓冲积压达到字节上限时先暂停读取、把缓冲解析完，避免“读得快、解析得慢”
   导致 buf 无限增长。未完成行缓冲超过 `config.SCAN_CACHE_BUF_LIMIT` 时跳过本次缓存
   写入并保留旧条目，避免大 base64 行反复编码落盘。单行解析异常只记录并跳过，
   不能让整批从同一位置反复回放。每 `config.LIVENESS_SCAN_SECONDS` 秒读取
   `CLAUDE_DIR/sessions/<pid>.json`，用 PID 存活校验外部 CLI 是否仍在运行（见「状态机」）。
2. **行解析**（`handle_line`）：逐行 JSON 解析，更新 `Session` 的 cwd/git 分支/模型/token 用量/
   工具统计/任务清单/状态等。事件经 `emit` 分发给 SSE 订阅者和可选 webhook
   （webhook 走单 worker 有界队列，容量 1000，满时丢弃新事件并限频记日志）；
   **首次扫描只回放不推送**（`first_scan_done` 标志）。
   Agent/Task 工具调用的子代理条目带 tid/state（running/done/error），tool_result 回传时翻转
   状态并发 `subagent_done`；**后台异步 Agent 例外**：`toolUseResult.isAsync` 的回传只是
   "已启动"确认，条目保持 running（标记 async、不解除 working），完成以后续
   `<task-notification>`（user 消息或新版 `queue-operation` 行的 `content` 字符串）
   按 `<tool-use-id>` 配对收尾，`turn_duration` 清扫跳过
   async 条目。父进程异常退出会留下截断转录（无 tool_result/turn_duration），
   `summary()`/`detail()` 在会话 stale 时把非 async 的运行条目输出为 `stale`
   （仅输出层副本，累计状态不动，前端渲染空心灰点）；async 条目无回合寿命，不降级。
   `parse_msg_line` 把 `<command-name>`/`<local-command-stdout>`
   系统行解析为 command/command_output part。
3. **HTTP 层**（`make_handler`，`ThreadingHTTPServer` + `BaseHTTPRequestHandler`）：
   `GET /` 渲染看板模板，`/sessions`、`/sessions/<id>`、`/sessions/<id>/messages` 返回 JSON，
   `/events` 是 SSE 流（连接先发 `snapshot`，随后推增量），`/static/` 提供静态文件。
   `GET /view?path=<绝对路径>` 渲染独立预览页（`preview.html`：md 渲染/文本/图片，
   页内相对链接按被预览文件所在目录继续跳 `/view`）；`GET /file?path=` 按类型返回
   文件内容，`&meta=1` 只回元信息（`fileview.classify_file` 四类；文本截断
   `config.PREVIEW_TEXT_CHARS`、只读头部字节，图片限 `config.PREVIEW_IMAGE_LIMIT`，
   svg 按文本防同源脚本，二进制流式下发带附件头；query 里的 `+` 先还原成
   `%2B` 再 parse_qs，文件名里的真实 `+` 不被吞成空格）。
   `/sessions/<id>/messages` 支持 `limit`（默认 100）、`before`、`from` 分页。
   会话列表/详情额外投影 `activity`（当前步骤）、`active_model`（当前模型）、
   `cli_live`（外部 CLI 进程仍存活），详情合并托管重试后的 `last_api_error`。
   托管进程通过 `POST /hook/PreToolUse` 接收 AskUserQuestion（仅回环 + 令牌）。
   `POST /sessions/<id>/approval` 设置审批模式，`POST /sessions/<id>/effort`
   设置托管思考档位（low/medium/high/xhigh/max/default），
   `POST /sessions/<id>/fast` 切换托管会话的 fast mode（body `{"fast": bool}`，
   仅托管支持，会话级不持久化）。
   `GET/POST /prompt-templates` 与 `DELETE /prompt-templates/<id>` 管理常用提示词模板，
   存 `CLAUDE_DIR/claude-sessions/prompt-templates.json`；每次操作前比对文件 mtime/size，
   运行期间手改文件会自动重载，损坏文件修复后也能自动恢复。网页发送框「模板」浮层支持搜索、
   插入光标处、保存当前输入、编辑与删除。
   `GET /system` 返回宿主机 CPU/内存快照（psutil），`POST /system/power` 执行重启/关机/睡眠（Popen 不等待、Windows 重启/关机带 /t 3 缓冲），
`POST /system/sleep-timer` 定时睡眠（服务端 `threading.Timer` 内存态，minutes 0-720、0 取消，
`GET /system` 的 `sleep_timer_remaining` 随轮询返回剩余秒）。
`POST /system/keep-awake` 开关「干活时推迟睡眠」（仅 Windows `SetThreadExecutionState(ES_SYSTEM_REQUIRED)`：
只挡 idle 自动睡眠，不唤醒已睡系统、不妨碍屏幕关闭与定时/手动睡眠；有非 stale 的 working 会话时持有，
15 秒评估一次，进程退出自动失效 fail-safe；开关持久化在 `claude-sessions/power.json`）。
`OrphanWatcher`（`orphans.py`）每 60 秒采样托管进程后代树（退出时 ppid 可能断链，故持续采样），
托管退出后仍存活的后代列为残留进程（PID 复用一律比对 create_time，只读短进程名），
`GET /system` 带 `orphans` 列表，`POST /system/orphans/end` 按 pid 或 all 发送 terminate。
4. **远程发消息/托管**（`send_message` + `Hosted`）：`POST /sessions/<id>/message` 用
   `claude -p --resume` 跑一轮（异步 job，`GET /jobs/<id>` 查询）；`POST /hosted` 启动
   stream-json 双向的常驻 claude 进程，消息立即写 stdin（忙时由 CLI 排队并在工具轮之间
   吸收），已托管会话的 message 请求自动路由到常驻进程。托管进程带 `--permission-prompt-tool stdio`，
   权限请求经 `/interactions` 审批；`POST /sessions/<id>/interrupt` 中断当前回合。
   stdout 的 `rate_limit_event` 存入 Hosted，投影为 `/hosted`、`/sessions`、detail 的
   `hosted_rate_limit`（`util.active_rate_limit` 过滤已过 `resets_at` 的记录）；
   `conversation_reset`（如 `/clear`）先记录并广播，随后按下一帧真实 `session_id`
   迁移 `Hosted.session_id`、`hosted_by_sid`、交互和 job，并发 `hosted_session_switched`。
   `system/task_started|progress|updated|notification` 探针仅在
   `CLAUDE_SESSIONS_DEBUG_TASK_FRAMES=1` 时打印。
   接口无鉴权，不要暴露到不可信网络。

### 关键设计决策

- **会话以文件路径为键**（`Monitor.sessions`）：同一 session id 可能出现在多个项目目录
  （恢复/移动过），按 id 键控会让两个文件的读取 offset 互相干扰。对外接口用
  `by_sid`/`unique_sessions` 按 id 去重（取最近活跃的）。
- **任务清单双来源**：从转录里的 `TaskCreate`/`TaskUpdate` 工具调用推断，但
  `~/.claude/tasks/<sid>/*.json` 目录存在时以它为准（`_merge_task_dirs`）。
- **对话消息按需增量提取**（`parse_msg_line`）：不随轮询解析，只在请求
  `/sessions/<id>/messages` 时解析；`Monitor.msg_cache` 以转录文件路径为键（同 SID
  多文件互不干扰），记录每个文件的读取 offset、已解析消息和 tool_use 索引，
  后续请求只解析新增部分（offset 按实际读取字节推进，stat 与 read 之间追加不重复解析）。
  超过 2 MiB 的转录首次只解析尾部窗口，`before` 翻页时按 1 MiB 块向前加载并前插，
  旧窗口里因 tool_use 在窗口外而独立展示的结果/备注此时并入对应工具条目；
  缓存最多保留 8 个最近使用的文件（LRU，活动请求不淘汰，超限允许暂时），并限制
  估算 payload 总量 64 MiB；文件数或字节超限时都淘汰最久未用缓存。每个文件有独立解析锁。
  跳过 sidechain（子代理对话）。工具结果合并行级 `toolUseResult`（diff/stdout/图片），
  用户与工具结果中的 base64 `image` block 保留为图片 part，并附 SHA-256 `image_id`
  供前端做轻量 DOM 签名；`toolUseResult.file.base64` 只是同图元数据，不重复渲染。
  `isMeta` 分 boilerplate / sourceToolUseID / origin 注入三种语义。
  工具 part 额外带 `input_data`（白名单字段：command/file_path/pattern/prompt 等）与
  `summary`，diff 结果带 `stats`（增删行数），异步 Agent 回执提取 `agent_id`；
  `system/compact_boundary`、`isCompactSummary`、`thinking` block 分别解析为
  compact / compact_summary / thinking part，前端折叠展示。
- **子代理会话**：`projects/<proj>/<parent>/subagents/agent-*.jsonl` 默认**不扫描**（懒加载），
  id 为 `<parent>::agent-<hash>`，默认不在 `/sessions` 返回；`?include_subagents=1`
  或请求子代理 id 时才解析，`--scan-subagents` 可在启动时全量扫描。只读、不能发消息或托管。
  父会话的 `subagent_count` 来自主转录的 Agent/Task 工具调用，不依赖子代理转录。
  子代理条目记录 `tid/state/async/type/description/prompt/model/agent_id`；前端把 Agent/Task
  工具调用渲染成子代理卡片，并在对话上方用后台任务条列出运行中的条目
  （所有宽度下只占一行：第一个 chip + `+N` 计数；点计数展开全部 chip，条首 ⌃ 收回，
  展开时限高 `min(30vh,240px)` 内部滚动，避免任务条吃掉大半屏）。
- **扫描缓存**：`scan_cache.py` 把每个转录的 offset、末尾 4 KiB 校验值和累计 Session 状态存到
  `CLAUDE_DIR/claude-sessions/scan-cache.json`；重启时校验一致就只读追加字节，截断/重写/缓存损坏
  时回退全量解析。默认只缓存主会话，开启 `--scan-subagents` 时降低落盘频率。
- **worktree 归并**（`project_root`）：`<proj>/.claude/worktrees/<name>` 视为属于 `<proj>`，
  看板按项目分组依赖此逻辑。
- **状态机**：`working`（有活动且助手未完成回复）/ `idle`（`system/turn_duration`；
  该会话从未出现该标记时才回退到 `stop_reason=end_turn`；turn_duration 清扫时
  `active_agents` 收敛到仍运行的异步 Agent）/ `stale`（超过 `--stale`
  秒无内容事件，在 `summary()` 输出时动态计算）。
  重试耗尽的 `assistant/isApiErrorMessage` 是**回合终态**（CLI 不再写
  `turn_duration`）：记录错误、清 pending、按 `_end_turn` 收尾，否则会话会永久停在
  working。该消息的 `message.model` 是 `<synthetic>`，不能覆盖真实模型。错误终态
  **不推进 `last_turn_end_ts`**，前端不把它算作“新回复”/未读，通知按失败类展示
  `turn_finished.error`。主转录中的 sidechain 错误属于子代理，不影响主会话；子代理
  自身转录则直接收尾为 idle。
- **活动心跳**（`Session.activity`）：每行转录同时记录当前步骤
  `{kind, label, since, tool/attempt/max_retries}`——用户输入/tool_result 后是
  「等待模型响应」，tool_use 是「执行 <工具>」，assistant 文本是「模型输出中」，
  api_error 是「模型服务重试中」，回合结束清空。网页把它显示在发送区上方的浮动胶囊里，
  本地每秒刷新已持续时长，长工具/长等待时不会只剩一个静止的「助手」。
- **存活探测**（`liveness.read_cli_sessions`）：外部 CLI 在
  `~/.claude/sessions/<pid>.json` 登记 sessionId/pid；每次都校验 PID 仍存活
  （Windows 用 OpenProcess，不能用会杀进程的 `os.kill(pid, 0)`），并在 Windows 上
  用 `procStart` 与进程创建时间比对排除 PID 复用。Monitor 自己发起的非托管一次性
  任务（`self.busy`）不在注册表里，也计入存活集合。进程存活时，只有仍有
  `pending_tools`/`active_agents` 的 working 会话才豁免 `--stale`（长 Bash/长思考
  不被误判）；曾确认存活过（`seen_live`）之后进程消失，则 `LIVENESS_EXIT_GRACE`
  秒无事件即按中断收尾，不必等满 `--stale`。`seen_live` 不跨服务重启恢复，
  避免误伤不写注册表的旧版 CLI。
- **API 错误识别**：`util.api_error_fields` 统一解析 `system/api_error`、
  `system/api_retry` 与合成 assistant 错误消息；文本优先取 `error.formatted`
  （如 DNS/网关提示），而不是只有 "Connection error." 的 `error.message`，并归类为
  `network` / `rate_limit` / `server` / `auth` / `other`，保留 status/code/重试次数/
  退避毫秒。前端横幅按类型给标题（「无法连接模型服务」等）、重试次数与倒计时，
  SSE `api_error` 到达即更新；正常 assistant 输出清除，错误终态保留。
  托管进程 stdout 的 `api_retry` 通过 `merge_hosted_retry` 合并进会话详情：
  取转录记录与 stdout 中较新的一条，回合结束（`last_turn_end_at >= retry.ts`）后
  只留作诊断，不再显示“正在重试”。
- **模型投影**：`Session.model` 表示最近一条真实回复的模型（跳过 `<synthetic>`）；
  列表/详情的 `active_model` 表示当前实际生效的模型（托管 `process_model` 优先，
  其次用户保存的选择，最后回退 `model`），网页展示用它，切换模型后立即反映。
- **双时钟**：`last_input_ts` 只由真实用户输入推进（排除 tool_result、isMeta、
  origin/promptSource 注入、中断标记），`last_event_ts` 是最后一条带时间戳的转录事件；
  列表按 `last_input_ts` 排序、回退 `last_event_ts`。
- **claude 进程启动**：Windows 上默认把 `claude` 解析到原生 `claude.exe`
  （shim 同目录 → PATH → `~/.local/bin/claude.exe`），显式 `--claude-bin` 保持原样并告警；
  `.cmd/.bat` 只能经 cmd.exe 执行、参数会被二次解析且超时只杀到 cmd.exe。子进程统一用
  `util.child_env` 去掉 `CLAUDECODE`、父会话 id、消息 socket/token 等变量，
  保留 `ANTHROPIC_*`、代理等用户配置，并把 `PWD` 对齐到 `cwd`。
- **托管审批**：模式为 `ask`/`auto`/`full`，**默认 `full`（服务自动放行）**，持久化在
  `~/.claude/claude-sessions/approvals.json`；已有明确保存的模式优先。`ask` 等待网页响应
  （`--permission-timeout` 默认 1800 秒，超时自动拒绝），`auto` 交给 CLI 权限模式。
- **思考档位（effort）**：仅托管会话；`POST /sessions/<id>/effort` 在线切换
  （control `apply_flag_settings` 的 `effortLevel`，`default` 传 null 回落模型默认），
  档位持久化在 `~/.claude/claude-sessions/efforts.json`，重托管 spawn 带 `--effort`（有档位时
  覆盖用户 `--claude-args` 里的同名参数）；在线切换失败不回滚保存，重托管后生效。
  托管进程 `system/init` 的 `slash_commands` 去前缀去重存入 Hosted，detail 返回 `commands`；
  发送框 `/` 单 token 输入时浮出提示面板（↑↓/Tab/Enter 补全、Esc 关闭），面板展开时
  Enter 只把选中项补全进输入框、不触发发送，收起后 Enter 才发送。新建会话页与未托管
  会话没有 init 广播，提示目录来自 `scan_slash_commands` 的静态扫描（`GET /commands?cwd=`
  供新建页、detail 在无活动托管进程时回退）：用户级 `<claude_dir>/commands` 与项目级
  `<cwd>/.claude/commands` 的 `*.md`（子目录映射 `name/sub`）、两级 `skills/` 下的目录型
  skill（含 `SKILL.md`，散装 `.md` 是知识库不算）、`installed_plugins.json` 里插件的
  `commands/*.md` 与目录型 skill（`plugin:name`，local scope 按 `projectPath` 匹配
  cwd）；内置命令不在文件系统上，仍要等进程 init 广播。结果按 `(claude_dir, cwd)` 缓存
  30 秒，前端另按 cwd 缓存。
- **fast mode（`/fast`）**：仅托管会话，会话级、不持久化（CLI 自身语义即 "this session only"，
  与计费相关，重托管回到关闭）。CLI 在 SDK（stream-json）模式下默认 `sdk_opt_in_required`，
  opt-in 靠 flagSettings（`--settings` 或 `apply_flag_settings` control）里的 `fastMode: true`，
  且 `/fast off` 会把已注入的 opt-in 一并清掉——因此 `Hosted.set_fast(True)` 每次开启前都先用
  `apply_flag_settings` 重新注入（幂等），再经正常消息通道发 `/fast on|off`，切换结果以命令
  气泡呈现在对话里（可能附带模型切换到 Opus 的提示；`process_model` 不跟随，后续带模型的
  发送仍按原记录切换）。状态分两层：`Hosted.fast_mode` 记录服务侧开关意图（前端 checkbox 的
  数据源），CLI 在 `system/init` 与 `result` 帧上报的 `fast_mode_state`
  （off/cooldown/on）与 `fast_mode_disabled_reason`（开放集合）由 `_update_fast_mode` 跟踪、
  变化时发 `hosted_fast_mode` 事件，detail 投影为 `fast` + `hosted_fast_mode`，前端头部 chip
  显示 on/cooldown。两者分开是因为 state 混入了模型支持与账号可用性（模型不支持时
  `/fast on` 成功但 state 仍为 off），不能当开关用。
- **工作时发送**：非托管一次性消息进入 `Monitor.remote_queues`，上一条 `_run_job` 结束后自动
  派发下一条（每会话上限 32）。托管会话的消息**立即写入常驻进程 stdin**，不再等服务端等上一轮
  结束：CLI 自己维护命令队列，并在工具轮之间把新消息折叠进当前回合（absorbed_mid_turn）。
  initialize 声明 `rapidFollowupPreempt`，回合还没输出内容时新消息可直接接管当前回合
  （`system/turn_preempted`；被接管的 job 记为 done「已被后续消息接管」，不报中断失败）。
  模型相同的追发完全并发；请求模型与进程当前模型不同且正忙时，这条消息等服务端切模型，
  避免改掉正在跑的回合的模型。`Hosted.pending` 同时容纳已写入 CLI 的消息，
  `_claimed` 表示发送线程已领取、`_writing` 表示正在写 stdin（此窗口取消返回 409）、
  `_written` 表示已交给 CLI。
  网页发送按钮只在 POST 进行中防重复点击，运行/排队中仍可继续发送；“托管（常驻进程）”
  默认勾选，网页发送始终以 `force` 绕过 working 检查（不再提供勾选框）。新建会话的托管模式也会把
  对话框里的审批选择作为首轮 `/hosted` 的 `approval` 提交。
  回合生命周期以 `command_lifecycle` 为准，`result` 作为旧版兜底；
  `queued/started/running/released` 是非终态，只有
  `completed/cancelled/discarded/refused/dropped` 结束回合。`result.user_message_uuids`
  列出该回合消费的全部消息，用于批量收尾；同一回合只发一次 `hosted_turn_done`。
  这些字段属于 CLI 的 `@internal` 协议：initialize 被旧版明确拒绝时退回基础 initialize；
  没有 uuid 列表时按提交顺序认领 result；旧版 CLI 只会退化为 CLI 内部排队，发送仍可用。
- **图片消息**：`/message`、`/new`、`/hosted` 接受可选
  `images: [{"media_type", "data"}]`（纯 base64），`message_content` 统一校验并生成
  文字在前、图片在后的 content blocks；托管直接写数组，一次性消息为此切到双向
  stream-json（`--verbose`），逐行读取 stdout 并以最终 `result` 事件收尾。
  支持 PNG/JPEG/GIF/WebP，每次最多 8 张、单张原始数据 3.75 MiB、合计 20 MiB，
  job/队列只保留文字与图片数量摘要，不复制 base64。网页支持选择/粘贴、缩略图和纯图片发送，
  附件读取按会话串行并在 pending 时禁发；202 后按提交项身份清理，保留后续新增附件。
- **队列查询与单项取消**：`GET /sessions/<sid>/jobs` 投影执行、等待及最近终态；取消只对
  尚未开始的消息生效——还没写入的从本地队列移除，已写入 CLI 队列的用
  `cancel_async_message`（`message_uuid` = job 的 `command_uuid`）撤回，CLI 回答
  `cancelled:false` 或 job 已被领取/正在执行时返回 409；取消排队项不计 turns、不发 turn_done。
  中断发送 `interrupt` 时带 `cancel_queued:true`，同时清空本地未终态消息。
  非托管 job/队列归 `Monitor.lock`，托管归 `Hosted.ready`；注册表与终态历史归 Monitor，
  嵌套锁仅允许 Hosted → Monitor。全部活动 job 保留，全局只保留最近 100 个首次终态，
  每会话最多返回其中 20 条；`started_at` 保持提交时间语义。队列和历史不跨服务重启恢复。
  `queue_changed` 仅提示刷新，GET/轮询负责对账；前端按 job ID 和提交 token 隔离反馈。
- **会话整理**：`SessionMetadata` 独立原子保存 `session-metadata.json`，按 sid 存别名、置顶、
  归档及持久递增 revision；不改转录标题或扫描缓存。HTTP/SSE 使用统一元数据投影，前端按
  `metadata_revision` 合并，旧响应不能回退新设置。归档只影响默认列表，不停止任务、不删转录，
  直链及恢复保留；全局待审批来自完整会话集，包含已归档会话。
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
  托管限流状态在会话卡片/头部显示 chip（`resets_at` 过期自动隐藏）；`/clear` 后收到
  `hosted_session_switched`，等新会话被扫描进列表再自动切换视图。
  对话只加载最近 100 条，消息列表顶部按钮加载更早；`cursor` 记录已加载末尾，
  服务端前插旧块时用响应里的 `shift` 调整，避免增量漏消息。
- **通知与连接健康**：SSE 慢消费者队列满时标记 `overflowed` 并断开连接（不再静默丢事件），
  客户端重连走 `snapshot` 全量对账；keepalive 为无 id 事件帧（15 秒，游标粘性不受影响），
  前端看门狗 90 秒无帧重建连接、回前台 45 秒检查（系统睡眠/网络切换造成的假死无错误事件），
  后台不做定时重连。浏览器通知（header 🔔 开关存 localStorage `notify`，权限在用户手势里申请）
  只由实时 SSE 事件驱动、仅页面隐藏时发送（新回复 `turn_finished` / 待审批 `interaction_pending` /
  失败 `hosted_turn_done`、`remote_message_done`，按 kind+sid+id 5 秒冷却去重，覆盖两路重复投递），
  重连对账与轮询不补发，点击通知聚焦并 `navigate` 到对应会话；
  无 Service Worker/推送/唤醒锁，系统睡眠零干预，睡眠期间错过的事件只更新看板不补通知。
  短回合过滤：`turn_finished` 与 `hosted_turn_done` 带 `duration`（回合秒数），
  系统面板可选 10/30/60 秒阈值（localStorage `notify_min_duration`，默认 30），
  仅过滤新回复类通知，待审批与失败始终提醒。
  标题栏显示 `(未读+待审批数)` 前缀（`updateDocTitle`）。
- **URL 路由**：看板视图与地址栏同步，`/p/<项目路径>` 是项目列表，
  `/p/<项目路径>/s/<会话ID>` 是对话，`/p/<项目路径>/new` 是新建；项目路径保留目录层级
  （反斜杠归一为 `/`，冒号保留，仅转义空格等特殊字符），旧的整体编码链接仍可解析。
  `/pending` 为全局待处理会话列表（header「待审批 N」徽章入口），只导航到原审批详情、不复制表单；
  `/hosted` 为托管会话列表（header「托管 N」徽章入口，点击卡片进对话）。
  `GET /pending`、`GET /hosted`、
  `GET /p/...` 与 `/` 一样回退渲染同一 SPA 模板。导航统一走 `navigate`（`pushState`），
  首页自动选中项目时用 `replaceState` 避免多余历史记录；`popstate` 重新解析地址并恢复视图。
  会话页「返回」回到进入来源：`navigate` 从非会话视图进入会话时把旧视图存进
  `sessionReturn`（托管/待处理/项目列表），返回按钮按它导航、标签随来源动态化
  （「返回托管列表」等）；会话间切换（`/clear` 换 sid）不覆盖来源，直链或整页刷新
  进入没有来源（内存态），回退默认项目列表。

### 前端

`templates/index.html` 是自包含 SPA：左侧项目列表、右侧会话卡片/对话视图，
对话用 marked + DOMPurify 渲染 markdown（用户气泡 / 助手 markdown / 可折叠工具结果与 diff），
消息按连续角色分组渲染：`renderConvList` 按组内容签名复用未变化的 DOM 节点，
markdown/DOMPurify 只处理新增或变化的组（无 DOM 原语的环境退回整段 HTML）。
消息内 AI 写的文件链接在线预览：消息组 DOM 新建时 `rewriteConvLinks` 把无 scheme 的
相对 href（含 `../`、盘符、`file:///`）按**会话 cwd** 解析成绝对路径
（`localPathFromHref`，盘符先于 scheme 判定），重写为 `/view?path=` 新标签打开；
浏览器错误解析出的遗留地址由 `renderMain` 兜底——`<已知项目>/s/<相对文件路径>` 多段
形态走 `routeFileUrl`，`/s/README.md` 这类带扩展名单段按 `looksLikeFileName` 同步判定
（会话 ID 均为 UUID / `parent::agent-hash`，永不带点号，零探测请求）直接跳预览。
自动刷新间隔存在 localStorage，对话视图仅在文件 size 变化时重渲染并保持滚动位置。
自动刷新间隔存在 localStorage，对话视图仅在文件 size 变化时重渲染并保持滚动位置。
对话视图是聊天窗口布局（`main.convview` 纵向 flex，类由 `renderMain` 进入/离开会话视图时
显式切换，不依赖 `:has()`）：固定头部单行（返回与标题同行，
cwd 与「对话 N/M」计数并入概览折叠 `overviewHtml`——summary 一行摘要、展开完整统计），待审批
卡片在其下；顶部区 `.conv-top` 限高 `60vh` 内部滚动（`main` 是 `overflow:hidden`，
不限高会让长审批卡片的底部按钮被裁掉），`#conv` 滚动区按旧→新渲染消息，
发送区固定底部。进入会话滚到底部；增量刷新贴底时自动跟底、翻看历史时不打扰；加载更早
按高度补偿保持视野。置顶/归档/别名不在会话页显示，只在外层列表卡片操作（别名暂无前端入口，
后端 API 保留）。发送队列摘要内联为模型选择行右侧的 chip（`#queue`）：无执行/等待且无错误/通知时隐藏，
仅 chip 文字可点击（不做悬停/聚焦自动展开，键盘 Enter/空格等价于点击），详情浮层是 body
顶层的 `#queuepop`（`position:fixed` 按 chip 矩形定位并夹紧视口，滚动/ resize 重定位）：
不随 `main` 的 overflow 裁剪、也不被侧栏遮挡；点击展开/收起，外点或 Esc 收起，
取消按钮、最近结束折叠在浮层内（点击兼容触屏）。
发送框「模板」按钮打开 body 顶层 `#tplpop`（`role="dialog"`，模式同 `#queuepop`/`#syspop`，
按 `visualViewport` 夹紧可见区域）：支持搜索、插入光标处、保存当前输入、编辑/删除；浮层内点击
会重渲染导致事件目标脱离 DOM，用 `tplPopClickInside` 豁免本次外点关闭；离开会话视图或重建面板
时收起。保存/删除期间按钮禁用防重复提交；列表预览只渲染前 240 个码点，搜索/插入仍用全文。
窄屏（≤768px）发送区次要控件（审批/思考/发送选项/托管）收进 ⚙ 浮层——`composer-extra`
桌面 `display:contents` 原样展开、控件 DOM 不移动；textarea 压两行，头部隐藏 cwd。
模型选择行强制单行（`nowrap`）：隐藏「模型选择」标签文字、select/自定义输入可收缩、
状态与队列 chip 单行省略并右对齐；发送/图片/模板/中断/⚙ 按钮同样收紧内边距保持一行。
窄屏顶部同样压缩：全局 header 隐藏「自动刷新/间隔/系统」文字与更新时间
（保留 `aria-label`/`title`），列表页用 `.hdr-break` 让托管/待审批/通知/系统徽章换行成两行；
**会话视图（`body.insession`）里徽章与通知/系统按钮一并隐藏、header 回到一行**（列表页均可达，
断连提示 `#connbadge` 仍会临时占行）。会话头「返回」「托管中」只留图标，概览摘要只显示对话数、
消息数、子代理与任务，模型/分支/token 留在展开面板（摘要项带 `ov-*` 类名，分隔符在项内，
隐藏后不留悬空「·」）。窄屏另有**沉浸模式**：会话头 ⛶ 按钮（仅窄屏渲染）切换 `body.zen`，
隐藏全局 header 与概览折叠行、消息区最大化；偏好持久化在 localStorage `zen`，由 `applyZen`
在 `renderMain` 切换 `convview` 后同步——仅会话视图生效，返回列表自动退出、重进自动恢复。
偏好与视觉态解耦：点击 token chip 展开概览只是**临时退出**（`zenSessionOff`，不落盘，
轮询刷新不翻回），离开会话视图或手动点 ⛶ 即失效，重进仍按偏好恢复；⛶ 按钮点亮与
aria 状态绑定当前视觉态。`autosizeComposer` 的输入框增高上限窄屏降为
视口 25%（桌面 40%），长草稿不再吞掉近半屏消息区。
所有设备的会话页发送框均常驻底部，滚动消息时不隐藏、不回弹，也不因滚动停止强制顶底。
发送状态（发送中/排队/处理中/已完成/失败）显示为输入区上方的浮动胶囊，不占布局高度；
没有发送任务时同一胶囊显示 `activity`（执行工具/等待模型/重试中）与已持续时长，
本地每秒刷新。「已完成/已取消」约 4 秒后自动消失，失败等错误保持显示。
会话头部 `#apierr` 按错误类型显示「无法连接模型服务 / 限流 / 异常 / 鉴权失败」，
带重试次数与倒计时；SSE `api_error` 即时更新、正常回复清除。`visibleApiError` 只在
working 或失败后 180 秒内把错误顶在头部，更早的历史失败留在对话里、不再伪装成当前
故障（避免打开旧会话时误以为服务仍不可用）。列表卡片另显示「模型重试」chip。
`turn_finished.api_error` 按失败通知（带 `error` 文本），不算新回复；
`hosted_api_retry` 直接使用后端归一化的 `text/kind`。托管流式气泡在收到工具调用
（`content_block_start` 的 tool_use）时清空，避免旧文本一直停在「助手正在输入…」。
发送区提供中断按钮、审批
模式选择与思考档位（effort）下拉（托管中 onchange 即切，未托管保存待下次托管生效；新建页未勾选
托管时禁用）与 fast 开关（仅会话页渲染，未托管禁用；切换失败回滚勾选，结果看对话里的命令气泡）；
审批默认 `full`，发送按钮在托管会话运行/排队期间保持可用。会话页和新建会话页共用
同一个 `composerHtml` 输入对话框；输入框下有斜杠指令提示面板（`wireCommandHints`，目录来自
detail 轮询的 `commands`，仅托管会话有），`<details>` 的展开状态按稳定 key 记录在 `detailsOpen`，
自动刷新/整页刷新后恢复（`sessionStorage`）。子代理折叠区与会话卡片显示运行中数量
（条目带状态圆点，运行中在前）；发送后对话末尾有本地回显气泡（`pendingEcho`，转录出现
同文本 user 消息或 command part 后退场，失败标红）；斜杠命令渲染为命令气泡（输出折叠），
状态行显示"正在执行命令 /xxx…"。工具卡由 `TOOL_VIEW` 注册表驱动（按 Bash/Read/Edit/Grep/
Agent 等给出摘要、类别色、状态与 diff 统计），连续同名工具折叠成一组；成功的 Read/Grep/Glob
结果默认不展开。消息头提供复制按钮，图片点击打开全屏灯箱；SSE 断开时 header 的
`#connbadge` 显示重连提示。模型行内的 `#tokchip` 显示上下文 token，点击展开概览。
移动端用 `visualViewport` 计算 `--keyboard-height` 收缩 body，输入框按内容自适应高度，
避免 iOS 键盘遮住发送区。
消息草稿按会话/新建项目隔离，仅存页面内存；发送响应只消费提交时版本，异步回调不能清空
其他面板或后续编辑的草稿；复用旧会话 DOM 时也要同步过期草稿版本，避免显示内容与待发送内容不一致。
新回复提示只依赖 `last_turn_end_at` 高水位推进，首次见会话只建基线，不以 `stale` 或工具结束
判断完成；消息展示水位覆盖已观察完成水位且页面可见才视为已读，旧响应、分页未追完或加载失败
不能消费新回复提示。对话加载还需请求实例与导航代次守卫，过期响应不能回退消息 DOM 或解除新请求锁。
会话详情与消息请求用带超时的 `fetchT` 并同时发起、分开等待：消息失败不拖垮会话页，
框架照常渲染、失败内联显示在消息区并随下一轮刷新自动重试；已渲染消息的刷新失败保持原样。
新建会话持续核对服务端 SID 校正；源提交槽清理与目标槽接管分开，不覆盖目标的新提交或抢回用户导航。
新建回显沿用点击提交时的时间与消息窗口，SID 发现/校正不能重置它们。
列表搜索只匹配已有会话元信息，条件按项目保存在内存；工具栏节点保持稳定，刷新只更新结果区，不打断焦点
或输入法。草稿、未读及搜索条件随整页刷新清空；别名/置顶/归档由独立元数据持久化。
归档范围与原搜索/状态条件取交集后稳定按置顶分组，组内保留原顺序；详情标题软更新，不重建输入区。
待处理页和队列摘要均不能因查看、取消或整理操作提前清除新回复提示。
header 右侧「系统」按钮弹出 `#syspop` 浮层（模式同 `#queuepop`）：CPU/内存利用率条形图、展开期间 3 秒自动刷新，底部重启/关机/睡眠经 `window.confirm` 二次确认后 POST；定时睡眠行按 `sleep_timer_remaining` 切换（未设定：预设下拉（30/60/120/180 分钟）+ 设定按钮，已设定：剩余时间 + 取消），轮询重写 innerHTML 时保留用户选中的预设值。另有「干活时推迟睡眠」开关（`#keepawake`）、短回合通知阈值（`#notifymindur`）与残留进程区块（`orphansHtml`，逐个/全部结束均 confirm 后 POST）。
