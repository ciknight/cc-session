# 正确性、性能与局部简化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复本轮审查确认的全部问题，并以 8 会话消息 LRU、锁外磁盘 I/O、单 worker webhook 和分组 DOM diff 完成性能优化及局部去重。

**Architecture:** 保持 monitor.py + session_preferences.py + 自包含 SPA 的现有结构。按文件路径隔离消息状态，以每路径扫描锁和每文件消息活动计数保护并发；公开 HTTP、存储和前端控件契约不变。

**Tech Stack:** Python 3.9+ 标准库、Jinja2、原生 JavaScript、unittest、node:test。

**执行约束：** 当前会话内联执行；不创建 worktree，不提交或推送。每个任务先写失败测试，再实现并跑对应测试。

## 补遗：计划遗漏的两项规格条目（Task 10 后补做）

自查设计文档发现以下两条在任务拆分时遗漏，已按同样 TDD 流程补齐：

- **SSE snapshot seq 先于数据采集分配**（设计 §一.6）：`_snapshot` 开头取 seq，
  采集期间 emit 的事件 seq 均大于 snapshot seq，客户端按 seq 去重不丢增量。
  测试 `test_sse_snapshot_seq_precedes_events_during_collection`。
- **`_handle_hook` 两处 `by_sid` 持 `mon.lock`**（设计 §一.6）：与扫描线程的
  sessions 字典变更并发时不再可能抛 RuntimeError。
  测试 `test_hook_resolves_session_under_monitor_lock`。

---

## 文件结构

- `monitor.py`：HTTP 安全、消息缓存、扫描锁、Agent/Interaction/Hosted/SSE/webhook 修复。
- `session_preferences.py`：偏好存储与 CLI 参数替换去重。
- `templates/index.html`：hosted 字段修复及分组 DOM diff。
- `tests/test_management_api.py`：HTTP 安全、消息缓存、SSE、静态文件与 webhook 测试。
- `tests/test_session_state.py`：Agent 生命周期测试。
- `tests/test_interactions.py`：Interaction、Hosted、hook 超时测试。
- `tests/test_transcript_parsing.py`：跨块工具结果合并测试。
- `tests/test_session_preferences.py`：偏好兼容与原子写入测试。
- `tests/test_frontend_controls.js`：hosted 兜底及 DOM 节点复用测试。
- `CLAUDE.md`：更新 LRU、扫描锁和 webhook 队列架构说明。

### Task 1: 统一写请求校验并修复静态目录边界

**Files:** `monitor.py:2683-2698,2757-2995,2908-2915`；`tests/test_management_api.py`

- [x] **Step 1: 写跨站执行入口与 static 前缀穿越的失败测试**

新增测试对 `/new`、`/hosted`、`/sessions/sid-1/message` 发送 `Content-Type: text/plain` 和恶意 Origin，patch 对应 Monitor 方法为 Mock，断言响应 403 且 Mock 未调用；DELETE 使用带 `hosted_id` 的 fake Hosted 并断言 `stop` 未调用。static 测试 patch `monitor.STATIC_DIR` 到临时 `static`，创建兄弟 `static-secret/secret.txt`，用原始 socket 请求 `/static/../static-secret/secret.txt`，断言 404。

```python
for path, method in (("/new", "new_session"), ("/hosted", "host_session"),
                     ("/sessions/sid-1/message", "send_message")):
    with patch.object(self.mon, method, return_value=(202, {})) as call:
        with self.assertRaises(HTTPError) as error:
            self.request(path, {"text": "hello", "cwd": str(self.project)},
                         {"Content-Type": "text/plain", "Origin": "http://evil.invalid"})
        self.assertEqual(error.exception.code, 403)
        call.assert_not_called()
```

- [x] **Step 2: 运行测试确认旧实现失败**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api -v`
Expected: 新执行入口测试返回非 403 或调用 Mock；static 测试返回 200。

- [x] **Step 3: 实现统一入口校验**

保留 `_safe_json_write` 的现有判断，将 Origin/Sec-Fetch-Site 部分提取为 `_safe_write_origin()`。`do_POST` 在 `/hook/` 分支之后、其他路由之前调用一次 `_safe_json_write`，删除分支内重复调用；`do_DELETE` 首先调用 `_safe_write_origin`。static 改为：

```python
root = STATIC_DIR.resolve()
fp = (root / name).resolve()
if not path_is_within(fp, root) or not fp.is_file():
    return self._json({"error": "not found"}, 404)
```

- [x] **Step 4: 运行本任务及完整管理 API 测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api -v`
Expected: 全部 PASS，原有同源写请求行为不变。

### Task 2: 修复消息缓存文件身份和 offset 追加竞态

**Files:** `monitor.py:2553-2630`；`tests/test_management_api.py`

- [x] **Step 1: 写两个真实 HTTP 回归测试**

在同一 Monitor 中注册 `a/same.jsonl` 与 `b/same.jsonl`，先后让 B、A 成为最新，断言两次 messages 响应不混文件。追加竞态用包装 Path 的 `stat()`：先记录 size，再在返回前追加一行；首次响应包含两行，第二次仍只能各出现一次。

```python
self.assertEqual(message_texts(first), ["B-old"])
self.assertEqual(message_texts(second), ["A-old", "A-new"])
self.assertEqual(message_texts(append_first), ["one", "two"])
self.assertEqual(message_texts(append_second), ["one", "two"])
```

- [x] **Step 2: 运行新增测试确认复现**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api -v`
Expected: 分别出现 `B-old + A-new` 和重复的 `two`。

- [x] **Step 3: 按路径键控并按实际读取量提交 offset**

在 messages 分支取得 `cache_key = str(s.path)`，msg_locks/msg_cache 均使用该 key。读取改为：

```python
with open(s.path, "rb") as f:
    f.seek(st["off"])
    raw = f.read()
data = st["buf"] + raw
msgs, st["buf"] = _parse_message_chunk(data, st["tools"])
st["msgs"].extend(msgs)
st["off"] += len(raw)
size = st["off"]
```

初始化和截断仍使用 stat size；缓存状态不包含另一文件的锁或游标。

- [x] **Step 4: 运行消息与转录测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api tests.test_transcript_parsing -v`
Expected: 全部 PASS，无消息混合或重复。

### Task 3: 合并跨块工具结果并实现 8 会话 LRU

**Files:** `monitor.py:88-118,391-396,2359-2390,2578-2628`；`tests/test_management_api.py`、`tests/test_transcript_parsing.py`

- [x] **Step 1: 写跨块合并与 LRU 失败测试**

用小的 MSG_TAIL_BYTES/MSG_CHUNK_BYTES 构造 tool_use 在旧块、tool_result 在尾部块的文件，加载更早后断言只有一个合并后的工具条目，不再存在独立 tool_result。LRU 测试访问 9 个文件，断言 `len(msg_cache) == 8`、最旧 key 被淘汰、当前 key 保留；用活动计数为 1 的条目验证不会提前淘汰。

- [x] **Step 2: 运行测试确认旧行为不符**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api tests.test_transcript_parsing -v`
Expected: 独立 tool_result 残留，msg_cache 为 9。

- [x] **Step 3: 增加反向合并辅助函数**

实现 `_merge_older_messages(st, chunk_msgs, chunk_tools)`：先合并列表与工具索引，再按 tool_use_id 将已有独立 result/note 并入目标 tool，删除被吸收的 part 和空消息；返回前插造成的索引净位移。所有 total/start/end/shift 在合并后计算，cursor 语义保持“已加载列表的末尾索引”。

- [x] **Step 4: 增加活动计数和 LRU 回收**

新增：

```python
MSG_CACHE_SESSION_LIMIT = 8
self.msg_lru = OrderedDict()  # cache key -> None，末尾最近使用
self.msg_active = Counter()   # cache key -> 正在处理的请求数
```

请求在 `msg_locks_guard` 下注册活动计数，finally 中减少计数、touch LRU，并从头淘汰 active 为 0 且非当前 key 的缓存、锁和 LRU 项，直到不超过 8；不得在持有 msg_locks_guard 时等待每文件锁。

- [x] **Step 5: 运行分页和并发相关测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api tests.test_transcript_parsing -v`
Expected: 全部 PASS，活动缓存无提前淘汰，游标测试不回归。

### Task 4: 修复 Agent 与 Interaction 生命周期

**Files:** `monitor.py:1035-1056,2194-2233`；`tests/test_session_state.py`、`tests/test_interactions.py`

- [x] **Step 1: 写同步/async Agent 与并发 resolve 测试**

同步 Agent 测试先进入 working，再发送两次 turn_duration，断言 active_agents 为空且 summary 为 idle；async Agent 测试断言 turn_duration 后 tid 保留，task-notification 后清除。Interaction 测试用两个线程和 Barrier 同时 resolve，断言成功数严格为 1。

- [x] **Step 2: 运行测试确认旧实现失败或暴露竞态**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_session_state tests.test_interactions -v`
Expected: 同步 Agent 仍 working；并发 resolve 在确定性交错下双双成功。

- [x] **Step 3: 实现状态收敛和实例锁**

turn_duration 在更新 subagents 后执行：

```python
active_async = {sub.get("tid") for sub in sess.subagents
                if sub.get("state") == "running" and sub.get("async")}
sess.active_agents.intersection_update(active_async)
```

Interaction 增加 `self.lock = threading.Lock()`，resolve 从状态检查到 event.set 全部放入 `with self.lock`。public 只读投影不持 Hosted/Monitor 锁，避免新增锁方向。

- [x] **Step 4: 运行生命周期测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_session_state tests.test_interactions -v`
Expected: 全部 PASS，async Agent 不被提前清理。

### Task 5: 修复 Hosted 关闭竞态与 hook 等待语义

> 实施中核实：POST /hosted 响应含 `resp["hosted_id"]`（monitor.py `_host_session_locked` 末尾追加），
> 前端 `j.hosted_id` 是有效字段，"hosted 字段不存在"为审查误报，前端无需改动。

**Files:** `monitor.py:859-866,1636-1654,2103-2128,2459-2480,2737`；`tests/test_interactions.py`、`tests/test_management_api.py`

- [x] **Step 1: 写关闭管道与 timeout=0 失败测试**

Hosted 测试在 stdin 已关闭时调用 `_write`，断言转换为 BrokenPipeError，并验证 stop 的 close 与 write 使用同一锁。hook 测试捕获 `Interaction.event.wait` 参数，permission_timeout=0 断言 None，正数断言原值；hook_client 捕获 opener.open 的 timeout。

- [x] **Step 2: 运行测试确认旧实现失败**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_interactions tests.test_management_api -v`
Expected: ValueError 漏出、stop 不等待 write_lock、hook 等待为 3600。

- [x] **Step 3: 实现写入/关闭同步和等待语义**

`_write` 在 write_lock 内将写入 ValueError 转为 BrokenPipeError；stop 只在 write_lock 内 close，释放锁后再 wait。hook_command 添加内部 `--timeout <seconds>`，hook_client 将 0 转为 `timeout=None`；服务端：

```python
timeout = mon.permission_timeout if mon.permission_timeout > 0 else None
inter.event.wait(timeout)
```

- [x] **Step 4: 运行 Hosted 与管理 API 测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_interactions tests.test_management_api tests.test_model_execution -v`
Expected: 全部 PASS，合法审批超时与新建路由不回归。

### Task 6: 将扫描与任务文件磁盘 I/O 移出 mon.lock

**Files:** `monitor.py:1242-1337,1459-1486`；`tests/test_scan_cache.py`、`tests/test_management_api.py`

- [x] **Step 1: 写锁外读取测试**

用事件控制的文件读取阻塞扫描线程，在读取尚未释放时，另一线程尝试 acquire `mon.lock`；新实现应立即成功，旧实现应超时。任务目录测试同样阻塞 `read_text`，并确认释放后任务合并结果一致。

- [x] **Step 2: 运行测试确认旧实现持锁等待**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_scan_cache tests.test_management_api -v`
Expected: 锁获取超时或测试按旧行为失败。

- [x] **Step 3: 增加每路径扫描锁并重组临界区**

Monitor 增加 `scan_locks` 字典，在 mon.lock 内仅取得锁对象；实际文件操作持有路径锁、不持 mon.lock。Session 注册/截断检查/offset 提交/行解析仍使用 mon.lock，提交 offset 用 `len(chunk)`。`_restore_or_new` 的 tail 读取在锁外执行。

`_merge_task_dirs` 先快照 `(key, sess, directory)`，锁外读取任务字典，再在 mon.lock 内确认 `sessions.get(key) is sess` 后更新。

- [x] **Step 4: 运行扫描、状态和缓存测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_scan_cache tests.test_session_state tests.test_transcript_parsing -v`
Expected: 全部 PASS，缓存恢复、截断和子代理懒加载不回归。

### Task 7: webhook 单 worker 队列与紧凑 JSON

**Files:** `monitor.py:88-118,377-423,967-991,2497-2506`；`tests/test_management_api.py`

- [x] **Step 1: 写顺序、transient 和紧凑响应测试**

patch `_post` 记录事件，emit 多个普通事件及一个 transient，等待 worker 排空，断言普通事件按序到达且 transient 不出现。队列测试使用小容量替身验证 put_nowait/Full 不抛给调用方。检查 `/healthz` 原始响应不含缩进换行，JSON 可正常解析。

- [x] **Step 2: 运行测试确认旧实现不符合**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api -v`
Expected: 旧实现无队列、响应带缩进。

- [x] **Step 3: 实现 worker 与紧凑序列化**

新增 `WEBHOOK_QUEUE_LIMIT = 1000`；配置 webhook 时创建 Queue 和 daemon worker。emit 用 put_nowait，Full 时累计并限频打印丢弃数；worker 顺序调用 `_post`。shutdown_event 设置后 worker 不无限等待新事件或网络。`_json` 改为：

```python
body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
```

- [x] **Step 4: 运行 HTTP 与事件测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api tests.test_interactions -v`
Expected: 全部 PASS，SSE 字段及事件内容不变。

### Task 8: 偏好存储与 CLI 参数替换去重

**Files:** `session_preferences.py:37-76,102-352`；`tests/test_session_preferences.py`

- [x] **Step 1: 运行现有偏好测试建立重构基线，并补临时文件清理测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_session_preferences -v`
Expected: 全部 PASS。新增测试模拟主要写入失败和临时文件 unlink 同时失败，断言对外仍为原 ValueError 文案而非清理 OSError。

- [x] **Step 2: 提取参数替换辅助函数**

实现 `_replace_arg(args, name, value)`；model_args/effort_args 只负责 None 快速返回、调用辅助函数及各自校验。保留剥掉 `--name value` 和 `--name=value` 后追加覆盖值的行为。

- [x] **Step 3: 提取原子写入和内部偏好基类**

`_atomic_json_dump(path, payload, prefix)` 统一 mkdir/mkstemp/fsync/replace/finally 清理；清理异常只吞掉，不覆盖主异常。内部基类实现 map 加载、get/set/bind、touched 和保存，Model/Effort/Approval 保留公开类名、字段名、默认 approval=auto、校验函数和中文错误文案；SessionMetadata 复用原子写入但保留 revision 逻辑。

- [x] **Step 4: 运行偏好、模型和 cc-switch 测试**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_session_preferences tests.test_model_execution tests.test_cc_switch -v`
Expected: 全部 PASS，现有 JSON 文件无需迁移。

### Task 9: 前端连续角色分组 DOM diff

**Files:** `templates/index.html:1195-1221,1397-1421,1436-1446,1550-1560`；`tests/test_frontend_controls.js`

- [x] **Step 1: 写节点复用与 markdown 调用次数失败测试**

构建三组消息并 render，保存节点引用；追加一组后再次 render，断言前三组节点严格相等、仅新增一组，marked.parse 只处理新增文本。前插历史和同角色末组扩张分别断言未变化组复用、只更新边界组。另测 detailsOpen、pendingEcho、liveBubble 和加载更早按钮仍存在。

- [x] **Step 2: 运行前端测试确认旧实现重建节点**

Run: `node --test tests/test_frontend_controls.js`
Expected: 旧节点引用不相等，markdown 重跑全部消息。

- [x] **Step 3: 实现纯分组函数与按需 HTML**

提取 `convGroups(messages)` 和 `convGroupHtml(group)`；convHtml 仍返回完整字符串以兼容现有测试/调用。组签名由 role、ts、parts 内容生成，DOM 节点将签名和源组保存在 JS 属性而非 HTML data 大字段中。

- [x] **Step 4: 在 renderConvList 同步独立消息组容器**

保持加载更早按钮、消息容器、echo、liveBubble 的顺序。同步时按签名复用并移动未变化节点，只给新/变化组调用 convGroupHtml；空容器初始化、前插、追加、同角色合并均走同一路径。不得整段重写 conv.innerHTML，不重建输入区。

- [x] **Step 5: 运行全部前端测试**

Run: `node --test tests/test_frontend_controls.js`
Expected: 全部 PASS，滚动补偿和草稿/回显测试不回归。

### Task 10: 文档、全量回归与页面冒烟

**Files:** `CLAUDE.md`；上述全部实现和测试文件

- [x] **Step 1: 更新架构说明**

在消息解析、轮询和 webhook 相关段落补充：缓存按文件路径键控、默认 8 会话 LRU、每路径扫描锁、磁盘 I/O 锁外执行、webhook 单 worker 容量 1000。保持文档简洁，不记录已修复 bug 的历史清单。

- [x] **Step 2: 运行全部 Python 测试**

Run: `PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v`
Expected: 所有测试 PASS，无未处理线程 traceback。

- [x] **Step 3: 运行全部前端测试**

Run: `node --test tests/test_frontend_controls.js`
Expected: 所有测试 PASS。

- [x] **Step 4: 启动本地服务做页面冒烟**

使用临时 CLAUDE_CONFIG_DIR 和非默认端口启动 monitor.py，请求 `/healthz`、`/`、一个会话 detail/messages 和 `/events` snapshot；确认进程正常关闭且没有新增错误日志。不调用真实模型。

- [x] **Step 5: 检查最终差异**

Run: `git diff --check; git status --short`
Expected: 无空白错误；只包含设计、计划、实现、测试和 CLAUDE.md 的预期改动。汇总测试数量、未运行的真实 CLI 冒烟及剩余限制。
