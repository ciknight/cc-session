# 会话监控正确性、性能与局部简化设计

## 目标

在保持现有 HTTP API、持久化文件格式、页面控件契约和轻量架构不变的前提下，完成本轮代码审查确认的全部正确性修复、性能优化与局部去重。实施采用分阶段就地改进，每类缺陷先有回归测试，再修改实现。

## 非目标

- 不引入 Web 框架、前端框架、ORM、新依赖或鉴权系统。
- 不拆分现有主模块，不改变 URL 路由、响应字段、队列状态机或转录格式。
- 不改变一次性消息超时策略，不运行会调用真实模型的 CLI 冒烟测试。
- 不追求与本轮问题无关的通用重构。

## 一、正确性修复

### 1. 写请求统一安全校验

将现有 Origin / Sec-Fetch-Site 检查与 JSON Content-Type 检查拆成可复用的写请求校验。`do_POST` 中除带独立令牌和回环校验的 `/hook/` 外，在路由分派前统一执行 JSON 写校验，删除各分支重复调用，覆盖 `/new`、`/hosted`、`/sessions/<sid>/message` 及原有写接口。`DELETE /hosted/<hid>` 执行 Origin 校验，不要求 JSON Content-Type。

跨站、`text/plain` 表单请求必须在任何偏好修改、进程启动或消息入队之前被拒绝。合法同源请求的现有行为不变。

### 2. 消息缓存文件身份与追加竞态

消息缓存和每文件解析锁改用 `str(Session.path)` 键控，不再使用 SID；响应中的会话 `id` 仍为 SID。相同 SID 的多个转录分别维护 offset、半行 buffer、消息和工具索引，`by_sid` 选择变化不能复用另一文件的解析状态。

增量读取记录实际从文件读取的字节数：

- `stat` 的 size 只用于判断是否需要读取以及初始化窗口。
- `read()` 后按原始字节长度推进 `off`，不能用此前的 size 覆盖。
- 响应 size 与本次消息缓存实际包含的文件位置一致，避免 stat/read 之间追加造成重复解析。

增加同 SID 的 A/B/A 切换和 stat 后追加两类 HTTP 回归测试。

### 3. 跨块工具结果反向合并

向前加载旧块时，除了把旧块 tool_use 加入工具索引，还要扫描已有消息中的独立 tool_result / tool_note：

- 能找到新加载 tool_use 的结果并入对应工具条目。
- 已吸收的独立 part 从原消息删除；空消息一并移除。
- `shift`、start、total、cursor 等分页投影基于合并后的真实消息列表计算，前端仍按现有响应字段对账。
- 已正常合并的消息不重复处理，异步 Agent 与普通工具语义不变。

### 4. Agent 回合结束清理

`turn_duration` 清扫时，将 `active_agents` 收敛为仍为 running 且标记 async 的 Agent tid 集合。同步或中断后不再有结果的 tid 必须移除，不能简单清空仍在后台运行的 async Agent。清理后再按现有规则决定 idle/working。

测试覆盖：中断同步 Agent 后连续 turn_duration 能进入 idle；async Agent 跨回合仍保持 working，收到 task-notification 后才结束。

### 5. 审批与托管并发

- `Interaction` 增加实例锁，`resolve` 的状态检查、字段更新和 event.set 在同一临界区完成；用户响应、超时和进程退出只有一个能首次成功。
- `Hosted.stop` 与 `_write` 共用 `write_lock` 关闭/写入 stdin。`_write` 将已关闭管道产生的 ValueError 归入现有 BrokenPipeError/OSError 控制失败路径，避免 `_send_loop` 无 traceback 处理地死亡、后续消息永久排队。
- 保持既有锁方向 Hosted → Monitor，不新增反向嵌套。

### 6. SSE、hook 与静态路径

- snapshot 在采集数据前分配 seq；采集后发生的事件序号必须大于 snapshot，客户端不能因去重丢失增量。
- `_handle_hook` 两处 `by_sid` 均在 `mon.lock` 内取得 Session，锁外只做 emit。
- `_static` 复用 `path_is_within`，拒绝 static 同前缀兄弟目录的路径穿越。
- `--permission-timeout 0` 时服务端 hook 等待不主动计时；内部 hook command 传递等待参数，hook 客户端同步采用无限等待。正数超时仍自动拒绝。CLI 自身 hook 配置的上限不在本次扩展。

### 7. 新建托管字段名

前端只使用 POST `/hosted` 已存在的 `hosted` 字段保存 hosted id，并用于 job session 比较和 `/hosted` 列表匹配；不新增或改名 API 字段。测试确保 init 先于首轮完成时，兜底路径也能进入正确 SID。

## 二、性能优化

### 1. 有界消息缓存

默认最多保留 8 个最近使用的文件消息缓存，使用常量 `MSG_CACHE_SESSION_LIMIT = 8`，不新增配置项。

每次 messages 请求：

1. 在 `msg_locks_guard` 下取得文件锁并增加活动请求计数。
2. 在锁外等待/持有每文件锁完成读取、解析和分页。
3. 请求结束后更新 LRU，并淘汰非当前 key、活动计数为零的旧缓存及对应锁。

活动请求不能被删除；超过 8 个并发文件时允许暂时超限，请求结束后再收敛。淘汰只影响性能，重新访问时按现有尾部窗口逻辑重建，不改变 API 分页语义。

### 2. 扫描磁盘 I/O 移出全局锁

增加按转录路径复用的扫描锁，确保 scan_once、子代理懒加载对同一文件的读取和 offset 提交仍串行。文件 stat/open/read、缓存尾部读取以及任务目录 JSON 读取不持有 `mon.lock`。

Session 注册、文件截断判断、offset/buf 提交和行解析继续保持现有 `mon.lock` 状态语义；任务合并先读快照，再在锁内确认 Session 仍是同一实例后更新。由此减少慢磁盘对 HTTP 和审批路径的阻塞，同时避免为本次优化重写 Session 的全部并发契约。

### 3. webhook 单 worker 有界队列

配置 webhook 时使用容量 1000 的队列和一个 daemon worker，按事件产生顺序发送，不再每事件创建线程。队列满时丢弃新事件并累计、限频打印丢弃计数；transient 事件仍不进入 webhook。关闭时停止接受新工作并尽力处理已排队事件，不在关闭路径无限等待网络。

### 4. 紧凑 JSON 与前端分组 DOM diff

普通 JSON API 使用紧凑 separators，只改变空白，不改变字段和值。

对话渲染保留 `convHtml` 的纯 HTML 生成能力供现有调用/测试使用，同时提取连续角色分组逻辑。`renderConvList` 将加载更早按钮、消息组、pendingEcho 和 liveBubble 分区管理：

- 每个消息组保存内容签名，只有新组或内容变化的组才调用 markdown/DOMPurify 并创建 DOM。
- 未变化组复用原节点，前插历史、追加消息和同角色末组扩张不会重建其他组。
- 保持 detailsOpen、加载更早高度补偿、贴底跟随、未读确认和发送回显行为。
- 无 DOM 的现有前端测试环境仍可使用纯 HTML 函数，不为生产代码引入测试专用分支。

## 三、局部简化

在 `session_preferences.py` 内提取共享实现：

- 一个内部 map preference 基类负责文件加载、锁、touched、get/set/bind 和原子保存；Model/Effort/Approval 保留原公开类名、校验文案、默认值及字段格式。
- 一个内部原子 JSON 写入辅助函数供偏好基类和 SessionMetadata 使用，保留各自 payload、错误类型和错误文案；临时文件清理失败不能覆盖主要异常。
- 一个内部 CLI 参数替换函数实现 model_args / effort_args，保持 `None` 不覆盖用户参数、非空值覆盖同名参数的行为。

不为了复用而合并 ScanCache 的不同错误策略，也不改变公开存储属性之外的接口。

## 四、测试与验收

### 后端回归

- 所有写接口跨站/错误 Content-Type 无副作用；hook 令牌路径不受影响。
- 同 SID 多文件、stat/read 追加、跨块工具结果和 LRU 淘汰/活动请求保护。
- 同步与 async Agent 的 turn_duration 清理。
- Interaction 并发 resolve、Hosted close/write、SSE snapshot 序号、hook 字典并发和 static 前缀穿越。
- permission-timeout 0/正数、hosted 字段兜底、webhook 顺序与队列上限、紧凑 JSON。
- 偏好三种文件格式的读写、损坏文件、bind/touched、原子写入失败和参数替换兼容性。

### 前端回归

- 现有 97 项控件/状态对账测试全部保留。
- 追加、前插和同角色合并不重建未变化消息组；markdown 只处理新/变化组。
- details 展开、滚动位置、pendingEcho、liveBubble、加载更早和未读逻辑不回归。

### 最终验证

运行：

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```

启动本地服务做页面冒烟，确认看板、会话追加、历史分页、发送区和审批入口可加载。工作树只包含本次设计、计划、实现与测试改动；不提交、不推送，除非用户另行明确要求。
