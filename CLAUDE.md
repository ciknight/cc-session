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

主服务 `monitor.py` + 扫描缓存 `scan_cache.py` + 会话偏好 `session_preferences.py` +
cc-switch 适配 `cc_switch.py` + Jinja 模板 `templates/index.html` + 本地前端依赖
`static/marked.min.js`、`static/purify.min.js`（不依赖外网 CDN）。

### 数据流

1. **轮询线程**（`Monitor.run` → `scan_once`）：按 `--interval` 秒扫描 `CLAUDE_DIR/projects/*/*.jsonl`，
   每个 `Session` 维护文件 offset 增量读取，半行留在 buffer 下次拼接。
2. **行解析**（`handle_line`）：逐行 JSON 解析，更新 `Session` 的 cwd/git 分支/模型/token 用量/
   工具统计/任务清单/状态等。事件经 `emit` 分发给 SSE 订阅者和可选 webhook；
   **首次扫描只回放不推送**（`first_scan_done` 标志）。
   Agent 工具调用的子代理条目带 tid/state（running/done/error），tool_result 回传时翻转
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
   `/sessions/<id>/messages` 支持 `limit`（默认 100）、`before`、`from` 分页。
   托管进程通过 `POST /hook/PreToolUse` 接收 AskUserQuestion（仅回环 + 令牌）。
   `POST /sessions/<id>/approval` 设置审批模式，`POST /sessions/<id>/effort`
   设置托管思考档位（low/medium/high/default）。
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
- **子代理会话**：`projects/<proj>/<parent>/subagents/agent-*.jsonl` 默认**不扫描**（懒加载），
  id 为 `<parent>::agent-<hash>`，默认不在 `/sessions` 返回；`?include_subagents=1`
  或请求子代理 id 时才解析，`--scan-subagents` 可在启动时全量扫描。只读、不能发消息或托管。
  父会话的 `subagent_count` 来自主转录的 Agent 工具调用，不依赖子代理转录。
- **扫描缓存**：`scan_cache.py` 把每个转录的 offset、末尾 4 KiB 校验值和累计 Session 状态存到
  `CLAUDE_DIR/claude-sessions/scan-cache.json`；重启时校验一致就只读追加字节，截断/重写/缓存损坏
  时回退全量解析。默认只缓存主会话，开启 `--scan-subagents` 时降低落盘频率。
- **worktree 归并**（`project_root`）：`<proj>/.claude/worktrees/<name>` 视为属于 `<proj>`，
  看板按项目分组依赖此逻辑。
- **状态机**：`working`（有活动且助手未完成回复）/ `idle`（`system/turn_duration`；
  该会话从未出现该标记时才回退到 `stop_reason=end_turn`）/ `stale`（超过 `--stale`
  秒无内容事件，在 `summary()` 输出时动态计算）。
- **双时钟**：`last_input_ts` 只由真实用户输入推进（排除 tool_result、isMeta、
  origin/promptSource 注入、中断标记），`last_event_ts` 是最后一条带时间戳的转录事件；
  列表按 `last_input_ts` 排序、回退 `last_event_ts`。
- **托管审批**：模式为 `ask`/`auto`/`full`，**默认 `auto`（交给 CLI 权限模式）**，持久化在
  `~/.claude/claude-sessions/approvals.json`；已有明确保存的模式优先。`ask` 等待网页响应
  （`--permission-timeout` 默认 300 秒，超时自动拒绝），`full` 由服务自动放行。
- **思考档位（effort）**：仅托管会话；`POST /sessions/<id>/effort` 在线切换
  （control `apply_flag_settings` 的 `effortLevel`，`default` 传 null 回落模型默认），
  档位持久化在 `~/.claude/claude-sessions/efforts.json`，重托管 spawn 带 `--effort`（有档位时
  覆盖用户 `--claude-args` 里的同名参数）；在线切换失败不回滚保存，重托管后生效。
  托管进程 `system/init` 的 `slash_commands` 去前缀去重存入 Hosted，detail 返回 `commands`；
  发送框 `/` 单 token 输入时浮出提示面板（↑↓/Tab 补全、Esc 关闭），Enter 直发不校验。
- **工作时发送**：托管会话在 `working` 时消息进入 `Hosted.pending` 队列；非托管一次性消息
  进入 `Monitor.remote_queues`，上一条 `_run_job` 结束后自动派发下一条（每会话上限 32）。
  网页发送按钮只在 POST 进行中防重复点击，运行/排队中仍可继续发送；“工作状态下也发送”与
  “托管（常驻进程）”默认勾选，前者以 `force` 绕过 working 检查。新建会话的托管模式也会把
  对话框里的审批选择作为首轮 `/hosted` 的 `approval` 提交。
  回合生命周期以 `command_lifecycle` 为准，`result` 作为旧版兜底；
  `queued/started/running/released` 是非终态，只有
  `completed/cancelled/discarded/refused/dropped` 结束回合。
- **队列查询与单项取消**：`GET /sessions/<sid>/jobs` 投影执行、等待及最近终态；取消接口只在
  owner 锁内移除真实等待成员，已领取（包括切模型）返回 409，不能调用会增加 turns 的回合收尾。
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
  对话只加载最近 100 条，消息列表顶部按钮加载更早；`cursor` 记录已加载末尾，
  服务端前插旧块时用响应里的 `shift` 调整，避免增量漏消息。
- **URL 路由**：看板视图与地址栏同步，`/p/<项目路径>` 是项目列表，
  `/p/<项目路径>/s/<会话ID>` 是对话，`/p/<项目路径>/new` 是新建；项目路径保留目录层级
  （反斜杠归一为 `/`，冒号保留，仅转义空格等特殊字符），旧的整体编码链接仍可解析。
  `/pending` 为全局待处理会话列表，只导航到原审批详情、不复制表单。`GET /pending`、
  `GET /p/...` 与 `/` 一样回退渲染同一 SPA 模板。导航统一走 `navigate`（`pushState`），
  首页自动选中项目时用 `replaceState` 避免多余历史记录；`popstate` 重新解析地址并恢复视图。

### 前端

`templates/index.html` 是自包含 SPA：左侧项目列表、右侧会话卡片/对话视图，
对话用 marked + DOMPurify 渲染 markdown（用户气泡 / 助手 markdown / 可折叠工具结果与 diff），
自动刷新间隔存在 localStorage，对话视图仅在文件 size 变化时重渲染并保持滚动位置。
对话视图是聊天窗口布局（`main:has(>#conv)` 纵向 flex）：固定头部显示标题、待审批卡片与
概览折叠（`overviewHtml`，summary 一行摘要、展开完整统计），`#conv` 滚动区按旧→新渲染消息，
发送区固定底部。进入会话滚到底部；增量刷新贴底时自动跟底、翻看历史时不打扰；加载更早
按高度补偿保持视野。置顶/归档/别名不在会话页显示，只在外层列表卡片操作（别名暂无前端入口，
后端 API 保留）。发送队列融合进发送区（`#queue` 即摘要行容器）：无执行/等待且无错误/通知时隐藏，
否则常驻一行摘要，hover/聚焦/点击摘要行浮出详情（取消按钮、最近结束折叠；点击兼容触屏）。发送区提供中断按钮、审批
模式选择与思考档位（effort）下拉（托管中 onchange 即切，未托管保存待下次托管生效；新建页未勾选
托管时禁用）；审批默认 `auto`，发送按钮在托管会话运行/排队期间保持可用。会话页和新建会话页共用
同一个 `composerHtml` 输入对话框；输入框下有斜杠指令提示面板（`wireCommandHints`，目录来自
detail 轮询的 `commands`，仅托管会话有），`<details>` 的展开状态按稳定 key 记录在 `detailsOpen`，
自动刷新/整页刷新后恢复（`sessionStorage`）。子代理折叠区与会话卡片显示运行中数量
（条目带状态圆点，运行中在前）；发送后对话末尾有本地回显气泡（`pendingEcho`，转录出现
同文本 user 消息或 command part 后退场，失败标红）；斜杠命令渲染为命令气泡（输出折叠），
状态行显示"正在执行命令 /xxx…"。
消息草稿按会话/新建项目隔离，仅存页面内存；发送响应只消费提交时版本，异步回调不能清空
其他面板或后续编辑的草稿；复用旧会话 DOM 时也要同步过期草稿版本，避免显示内容与待发送内容不一致。
新回复提示只依赖 `last_turn_end_at` 高水位推进，首次见会话只建基线，不以 `stale` 或工具结束
判断完成；消息展示水位覆盖已观察完成水位且页面可见才视为已读，旧响应、分页未追完或加载失败
不能消费新回复提示。对话加载还需请求实例与导航代次守卫，过期响应不能回退消息 DOM 或解除新请求锁。
新建会话持续核对服务端 SID 校正；源提交槽清理与目标槽接管分开，不覆盖目标的新提交或抢回用户导航。
新建回显沿用点击提交时的时间与消息窗口，SID 发现/校正不能重置它们。
列表搜索只匹配已有会话元信息，条件按项目保存在内存；工具栏节点保持稳定，刷新只更新结果区，不打断焦点
或输入法。草稿、未读及搜索条件随整页刷新清空；别名/置顶/归档由独立元数据持久化。
归档范围与原搜索/状态条件取交集后稳定按置顶分组，组内保留原顺序；详情标题软更新，不重建输入区。
待处理页和队列摘要均不能因查看、取消或整理操作提前清除新回复提示。
