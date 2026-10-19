# SSE 实时更新、消息分页、流式文本与 AskUserQuestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在前一版托管审批/回合判定的基础上，加入 SSE 快照与增量、消息分页、流式文本、渲染缓存，以及 plan mode 与 AskUserQuestion 的网页交互。

**Architecture:** 后端 `Monitor` 负责事件序号/transient 事件、SSE 快照、消息分页切片、托管流式文本聚合和 hook 回调；`Hosted` 负责 `--include-partial-messages`、`--settings` 注册和流式状态。前端保持自包含 SPA，用 EventSource + 节流刷新 + 消息窗口，审批/计划/选择卡片共用 `#interactions` 容器。

**Tech Stack:** Python 3.9+ 标准库、Jinja2、原生 JS、unittest、node:test。

**Spec:** `docs/superpowers/specs/2026-10-20-sse-live-paging-ask-user-design.md`

**测试命令（每个任务结束都运行）：**

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```

---

### Task 1: 事件序号、transient 事件与 SSE 快照

**Files:**
- Modify: `monitor.py`（`Monitor.__init__`、`emit`、`make_handler._sse`）
- Test: `tests/test_management_api.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_management_api.py` 加入：

```python
    def test_emit_transient_does_not_touch_recent_or_webhook(self):
        self.mon.first_scan_done = True
        self.mon.webhook = "http://invalid.example/hook"
        q = queue.Queue()
        self.mon.subscribers.append(q)
        self.mon.emit(self.sess, "turn_delta", {"text": "hi"}, transient=True)
        self.assertEqual(list(self.sess.recent), [])
        event = q.get_nowait()
        self.assertEqual(event["type"], "turn_delta")
        self.assertEqual(event["seq"], 1)

    def test_sse_connection_starts_with_snapshot(self):
        response = urlopen(self.base + "/events", timeout=5)
        lines = []
        while True:
            line = response.readline().decode().rstrip("\n")
            lines.append(line)
            if line == "" and any(x.startswith("data:") for x in lines):
                break
        response.close()
        self.assertIn("event: snapshot", lines)
        data = json.loads([x for x in lines if x.startswith("data:")][0][5:])
        self.assertIn("sessions", data["data"])
        self.assertIn("interactions", data["data"])
        self.assertIsInstance(data["seq"], int)
```

`tests/test_management_api.py` 顶部补 `import queue`。

- [ ] **Step 2: 实现**

`Monitor.__init__` 增加：

```python
self.event_seq = 0
```

`emit` 改为：

```python
def emit(self, sess, kind, data, transient=False):
    if not self.first_scan_done:
        return
    self.event_seq += 1
    ev = {"ts": now_ts(), "seq": self.event_seq,
          "session": sess.id if sess else None, "type": kind, "data": data}
    if not transient and sess is not None:
        sess.recent.append(ev)
    for q in list(self.subscribers):
        try:
            q.put_nowait(ev)
        except queue.Full:
            pass
    if self.webhook and not transient:
        threading.Thread(target=self._post, args=(ev,), daemon=True).start()
```

`make_handler._sse` 在订阅后、循环前发送快照：

```python
def _snapshot(self, only):
    if only:
        with mon.lock:
            s = mon.by_sid(only)
            d = s.detail(mon.stale_after, mon.project_of(s, mon.project_map())) if s else None
        h = mon.hosted_by_sid.get(only)
        data = {
            "session": d,
            "interactions": mon.pending_interactions(only),
            "live": {"session_id": only,
                     "active": bool(h and h.alive() and (h.current or h.processing)),
                     "text": h.stream_text if h else ""},
        }
    else:
        with mon.lock:
            pmap = mon.project_map()
            sessions = sorted(((s.summary(mon.stale_after, mon.project_of(s, pmap))
                                for s in mon.unique_sessions()),
                               ), key=lambda x: x["last_input_at"] or x["last_event_at"],
                              reverse=True)
            counts = Counter(i.session_id for i in mon.interactions.values()
                             if i.state == "pending")
        for s in sessions:
            s["pending_interactions"] = counts.get(s["id"], 0)
            h = mon.hosted_by_sid.get(s["id"])
            s["hosted"] = bool(h and h.alive())
        data = {"sessions": sessions, "interactions": mon.pending_interactions()}
    mon.event_seq += 1
    return {"ts": now_ts(), "seq": mon.event_seq, "session": only,
            "type": "snapshot", "data": data}
```

在 `_sse` 中：

```python
q = queue.Queue(maxsize=1000)
mon.subscribers.append(q)
try:
    snap = self._snapshot(only)
    self.wfile.write(
        f"id: {snap['seq']}\nevent: snapshot\ndata: "
        f"{json.dumps(snap, ensure_ascii=False)}\n\n".encode())
    self.wfile.flush()
    while True:
        ...
        self.wfile.write(
            f"id: {ev.get('seq', 0)}\nevent: {ev['type']}\ndata: "
            f"{json.dumps(ev, ensure_ascii=False)}\n\n".encode())
```

注意：`_snapshot` 是 `H` 的方法，通过 `mon.event_seq` 分配序号；快照本身不经过 `emit`，因此不会进入 `recent`。

- [ ] **Step 3: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api -v
git add monitor.py tests/test_management_api.py
git commit -m "feat: SSE 快照与 transient 事件"
```

---

### Task 2: 消息分页

**Files:**
- Modify: `monitor.py`（消息接口）
- Test: `tests/test_management_api.py`

- [ ] **Step 1: 写失败测试**

```python
    def _write_transcript(self, count):
        lines = []
        for i in range(count):
            lines.append(json.dumps({
                "type": "user", "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": f"m{i}"}}))
        path = self.project / "sid-1.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.sess.path = path

    def test_messages_paging_default_and_before(self):
        self._write_transcript(250)
        first = json.load(self.request("/sessions/sid-1/messages"))
        self.assertEqual(first["total"], 250)
        self.assertEqual(len(first["messages"]), 100)
        self.assertEqual((first["start"], first["end"], first["has_more"]), (150, 250, True))
        older = json.load(self.request("/sessions/sid-1/messages?before=150&limit=50"))
        self.assertEqual((older["start"], older["end"]), (100, 150))
        self.assertEqual(older["messages"][0]["parts"][0]["text"], "m100")
        newer = json.load(self.request("/sessions/sid-1/messages?from=240"))
        self.assertEqual([m["parts"][0]["text"] for m in newer["messages"]],
                         ["m240", "m241", "m242", "m243", "m244", "m245", "m246", "m247", "m248", "m249"])

    def test_messages_paging_rejects_bad_params(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/sessions/sid-1/messages?before=1&from=2")
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/sessions/sid-1/messages?limit=0")
        self.assertEqual(error.exception.code, 400)
```

- [ ] **Step 2: 实现**

`Monitor.__init__` 增加 `self.msg_lock = threading.Lock()`。

消息接口在处理完缓存后改为：

```python
qs = parse_qs(u.query)
try:
    limit = int(qs.get("limit", ["100"])[0])
    before = int(qs["before"][0]) if "before" in qs else None
    frm = int(qs["from"][0]) if "from" in qs else None
except ValueError:
    return self._json({"error": "limit/before/from 必须是整数"}, 400)
if limit < 1 or limit > 500 or (before is not None and before < 0) \
        or (frm is not None and frm < 0):
    return self._json({"error": "分页参数超出范围"}, 400)
if before is not None and frm is not None:
    return self._json({"error": "before 与 from 不能同时使用"}, 400)

with mon.msg_lock:
    ... 现有的缓存读取/增量解析 ...
    total = len(st["msgs"])
    if before is not None:
        start, end = max(0, before - limit), min(before, total)
    elif frm is not None:
        start, end = min(frm, total), min(frm + limit, total)
    else:
        start, end = max(0, total - limit), total
    payload = {"id": sid, "size": size, "total": total,
               "start": start, "end": end, "has_more": start > 0,
               "messages": st["msgs"][start:end]}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
return self._raw(body)
```

缓存结构去掉 `body` 字段（或保留不用）。

- [ ] **Step 3: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api -v
git add monitor.py tests/test_management_api.py
git commit -m "feat: 会话消息分页"
```

---

### Task 3: 托管流式文本

**Files:**
- Modify: `monitor.py`（`Hosted`、`emit` 调用、`info`、详情接口）
- Modify: `tests/fake_claude.py`
- Test: `tests/test_interactions.py`

- [ ] **Step 1: 写失败测试**

fake 对 `text == "stream"` 分支：

```python
elif text == "stream":
    for chunk in ("你", "好", "世界"):
        emit({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": chunk}}})
    complete(uuid, "stream-done")
```

测试：

```python
    def test_stream_text_is_aggregated_and_cleared(self):
        self.mon.first_scan_done = True
        q = queue.Queue()
        self.mon.subscribers.append(q)
        host, job = self.start_host("stream")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertEqual(host.info()["stream_text"], "")
        deltas = []
        while not q.empty():
            ev = q.get_nowait()
            if ev["type"] == "turn_delta":
                deltas.append(ev["data"]["text"])
        self.assertTrue(deltas)
        self.assertIn("你", "".join(deltas))
```

`tests/test_interactions.py` 顶部补 `import queue`。

- [ ] **Step 2: 实现**

`Hosted.__init__` 增加：

```python
self.stream_text = ""
self.stream_job = None
self.last_delta_emit = 0.0
```

`start` 参数增加 `--include-partial-messages`。

`_send_loop` 在设置 `self.current = job` 时：

```python
self.stream_text = ""
self.stream_job = job
self.last_delta_emit = 0.0
```

`_read_out` 增加：

```python
elif typ == "stream_event":
    self._on_stream_event(obj)
```

`_on_stream_event`：

```python
def _on_stream_event(self, obj):
    event = obj.get("event") or {}
    if event.get("type") != "content_block_delta":
        return
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta" or not delta.get("text"):
        return
    with self.ready:
        job = self.current
        if job is None or self.stream_job is not job:
            return
        chunk = delta["text"]
        should = now_ts() - self.last_delta_emit >= 0.08
        if should:
            self.last_delta_emit = now_ts()
    if should:
        self._emit_session_event("turn_delta",
                                 {"hosted": self.hid, "job": job.get("id"),
                                  "text": chunk}, transient=True)
```

注意：`text` 是当前 job 的**完整聚合文本**（节流发送）；回合结束时再发一次最终文本，然后清空。前端直接用 `text` 覆盖气泡内容。`_finish_job` 清空前发送最终 `turn_delta`。

`_emit_session_event` 增加 `transient=False` 并透传。

回合结束清空：

- `_finish_job` 与 `_read_out` 退出清理中：
  ```python
  self.stream_text = ""
  self.stream_job = None
  ```

`info()` 增加：

```python
"stream_text": self.stream_text,
"streaming": bool(self.stream_job is not None),
```

`/sessions/<id>` 详情增加：

```python
d["hosted_stream_text"] = h.stream_text if h and h.alive() else ""
d["hosted_streaming"] = bool(h and h.alive() and h.stream_job is not None)
```

- [ ] **Step 3: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_interactions -v
git add monitor.py tests/fake_claude.py tests/test_interactions.py
git commit -m "feat: 托管回合流式文本"
```

---

### Task 4: 前端 SSE、分页与渲染缓存

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: SSE 连接与事件分发**

在脚本中增加：

```js
let esGlobal = null, esSession = null, lastSeq = 0;
let listTimer = null, convTimer = null;
const SSE_TYPES = ["snapshot", "session_discovered", "user_message", "assistant_message",
  "tool_use", "tool_error", "turn_finished", "subagent_start", "task_created",
  "task_updated", "context_compacted", "api_error", "hosted_turn_done",
  "hosted_api_retry", "hosted_foreign_turn", "interaction_pending",
  "interaction_resolved", "turn_interrupted", "turn_delta", "remote_message_sent",
  "remote_message_done", "hosted_exited"];

function scheduleListRefresh(){
  clearTimeout(listTimer);
  listTimer = setTimeout(() => { if(!document.hidden) load(); }, 800);
}
function scheduleConvRefresh(sid){
  if(view.session !== sid) return;
  clearTimeout(convTimer);
  convTimer = setTimeout(() => { if(!document.hidden) renderConversation(sid, false); }, 500);
}
function onSseEvent(event){
  let msg;
  try{ msg = JSON.parse(event.data); }catch{ return; }
  if(msg.seq && msg.seq <= lastSeq) return;
  if(msg.seq) lastSeq = msg.seq;
  if(msg.type === "snapshot"){
    if(msg.data.sessions){ sessions = msg.data.sessions; renderMain(); }
    if(msg.data.session && view.session === msg.session){
      applySessionDetail(msg.data.session);
      scheduleConvRefresh(msg.session);
    }
    return;
  }
  if(msg.type === "turn_delta"){
    if(view.session === msg.session) updateLiveBubble(msg.data.text);
    return;
  }
  scheduleListRefresh();
  if(msg.session) scheduleConvRefresh(msg.session);
}
function connectGlobalSse(){
  if(!window.EventSource) return;
  if(esGlobal) esGlobal.close();
  esGlobal = new EventSource("/events");
  for(const type of SSE_TYPES) esGlobal.addEventListener(type, onSseEvent);
  let opened = false;
  esGlobal.onopen = () => {
    if(opened){ lastSeq = 0; load(); if(view.session) renderConversation(view.session, true); }
    opened = true;
  };
}
function connectSessionSse(id){
  if(!window.EventSource) return;
  if(esSession) esSession.close();
  esSession = new EventSource("/events?session=" + encodeURIComponent(id));
  for(const type of SSE_TYPES) esSession.addEventListener(type, onSseEvent);
}
```

增加 `applySessionDetail(d)`：更新状态徽标和 `#infopanel`，与现有 `keep` 分支保持一致。`renderConversation` 首次创建面板时调用 `connectSessionSse(id)`；返回列表时：

```js
if(esSession){ esSession.close(); esSession = null; }
```

页面启动时在 `load()` 前调用 `connectGlobalSse()`。

- [ ] **Step 2: 对话分页状态**

增加：

```js
let convState = {id: null, msgs: [], total: 0, start: 0, size: -1, hasMore: false};
```

修改首次对话请求为：

```js
fetch(`/sessions/${encodeURIComponent(id)}/messages?limit=100`)
```

保存：

```js
convState = {id, msgs: m.messages.slice(), total: m.total, start: m.start,
             size: m.size, hasMore: m.has_more};
```

`keep` 分支刷新时：

```js
if(convState.id !== id || m.size < convState.size || m.total < convState.total){
  convState = {id, msgs: m.messages.slice(), total: m.total, start: m.start,
               size: m.size, hasMore: m.has_more};
  renderConvList();
} else if(m.total > convState.total){
  const r = await fetch(`/sessions/${encodeURIComponent(id)}/messages` +
    `?from=${convState.total}&limit=500`);
  const more = await r.json();
  if(more.messages.length){
    convState.msgs.push(...more.messages);
    convState.total = more.total;
    convState.size = more.size;
    renderConvList();
  }
}
```

`renderConvList()`：

```js
function renderConvList(){
  const el = document.getElementById("conv");
  if(!el) return;
  el.innerHTML = convHtml(convState.msgs.slice().reverse()) +
    (convState.hasMore ? '<div style="text-align:center;margin:10px 0">' +
      '<button class="back" id="loadearlier">加载更早</button></div>' : "");
  const btn = document.getElementById("loadearlier");
  if(btn) btn.onclick = loadEarlier;
}
async function loadEarlier(){
  const id = convState.id;
  if(!id || !convState.hasMore || convState.loadingEarlier) return;
  convState.loadingEarlier = true;
  const main = document.getElementById("main");
  const anchor = main.scrollHeight;
  try{
    const r = await fetch(`/sessions/${encodeURIComponent(id)}/messages` +
      `?before=${convState.start}&limit=100`);
    const page = await r.json();
    convState.msgs = page.messages.concat(convState.msgs);
    convState.start = page.start;
    convState.hasMore = page.has_more;
    renderConvList();
    main.scrollTop += main.scrollHeight - anchor;
  }finally{ convState.loadingEarlier = false; }
}
```

保留原有 `convCache.size` 的用途或移除；`renderConvList` 使用 `convState`。

- [ ] **Step 3: 渲染缓存**

`renderSidebar`：

```js
function renderSidebar(projects){
  const html = projects.map(...).join("") || '<div class="empty">暂无项目</div>';
  if(html === lastSidebarHtml) return;
  lastSidebarHtml = html;
  document.getElementById("projs").innerHTML = html;
}
```

`renderSessionList` 同理，先计算完整 HTML 再比较；只有真正写入时调用 `bindCc`。注意 `curPanel` 仍需在首次渲染时设置。

- [ ] **Step 4: 测试**

在 `tests/test_frontend_controls.js` 增加：

- `convHtml` 分页数据渲染；
- `loadEarlier` 的拼接逻辑（mock fetch）；
- `renderSidebar` 相同 HTML 不重复写 DOM（记录 innerHTML 赋值次数需要 mock setter；可跳过，改为测试 `lastSidebarHtml` 缓存命中后内容不变）；
- `updateLiveBubble` 使用 `esc`（文本节点或 innerHTML 转义）。

- [ ] **Step 5: 服务端与前端联调**

运行全部测试和 `uv run monitor.py` 手工打开页面，确认：
- 新会话/新消息在 1 秒内出现；
- 断网重连后数据一致；
- 「加载更早」保持滚动位置。

提交：

```bash
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 前端 SSE、分页与渲染缓存"
```

---

### Task 5: 流式临时气泡

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 实现**

```js
let liveText = "", liveRaf = null;
function updateLiveBubble(text){
  liveText = text || "";
  if(liveRaf) return;
  liveRaf = requestAnimationFrame(() => {
    liveRaf = null;
    renderLiveBubble();
  });
}
function renderLiveBubble(){
  const conv = document.getElementById("conv");
  if(!conv) return;
  let el = document.getElementById("livebubble");
  if(!liveText){
    if(el) el.remove();
    return;
  }
  if(!el){
    el = document.createElement("div");
    el.id = "livebubble";
    el.className = "msg assistant";
    el.innerHTML = '<div class="mhead">助手 · 正在输入…</div><div class="abody"></div>';
    conv.insertBefore(el, conv.firstChild);
  }
  const body = el.querySelector(".abody");
  if(body) body.textContent = liveText;
}
function clearLiveBubble(){ liveText = ""; renderLiveBubble(); }
```

- 在 `turn_finished`、`hosted_turn_done`、`interaction_pending` 的事件处理中调用 `clearLiveBubble()`（对于当前会话）。
- 对话全量刷新后调用 `clearLiveBubble()`。
- `requestAnimationFrame` 在 Node 测试中不存在；测试时用 `setTimeout` 回退：
  ```js
  const raf = window.requestAnimationFrame || (fn => setTimeout(fn, 16));
  ```

- [ ] **Step 2: 测试并提交**

```bash
node --test tests/test_frontend_controls.js
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 流式文本临时气泡"
```

---

### Task 6: Plan mode 卡片

**Files:**
- Modify: `monitor.py`（`Interaction`、`permission_preview`）
- Modify: `templates/index.html`
- Test: `tests/test_interactions.py`、`tests/test_frontend_controls.js`

- [ ] **Step 1: 服务端**

`Interaction.__init__` 增加：

```python
self.kind = "plan" if self.tool_name == "ExitPlanMode" else "permission"
self.questions = []
self.answers = None
```

`public()` 增加 `kind`、`questions`。

`permission_preview` 增加：

```python
if tool_name == "ExitPlanMode":
    plan = ti.get("plan")
    return {"kind": "text",
            "text": clip(plan if isinstance(plan, str) else json.dumps(ti, ensure_ascii=False), 8000)}
```

fake 增加 `text == "plan"` 分支，发出 `ExitPlanMode` 请求。

测试：

```python
    def test_exit_plan_mode_is_plan_kind(self):
        host, job = self.start_host("plan")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        pending = self.mon.pending_interactions("fake-session")[0]
        self.assertEqual(pending["kind"], "plan")
        self.assertFalse(pending["allow_always"])
```

- [ ] **Step 2: 前端**

`interactionsHtml` 在 `kind === "plan"` 时渲染：

```js
`<div><b>计划待确认</b></div>
 <pre class="tool-out">${esc(preview.text || "")}</pre>
 <div style="margin-top:6px;display:flex;gap:8px">
   <button class="back" data-act="allow" data-scope="once">批准计划</button>
   <button class="back" data-act="deny" data-reason="The user wants to keep planning.">继续规划</button>
 </div>`
```

`respondInteraction` 支持 `act` 与可选 `reason`：deny 时把 reason 写入 body。

- [ ] **Step 3: 测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_interactions -v
node --test tests/test_frontend_controls.js
git add monitor.py templates/index.html tests/fake_claude.py tests/test_interactions.py tests/test_frontend_controls.js
git commit -m "feat: plan mode 审批卡片"
```

---

### Task 7: AskUserQuestion 的 Interaction 模型

**Files:**
- Modify: `monitor.py`（`Interaction`、`respond_interaction`）
- Test: `tests/test_management_api.py`

- [ ] **Step 1: 实现**

`Interaction.__init__` 签名改为：

```python
def __init__(self, request_id, hosted, request, session_id=None,
             kind=None, questions=None):
```

- `hosted` 可以为 None。
- `self.hosted_id = getattr(hosted, "hid", None)`
- `self.session_id = session_id or getattr(hosted, "session_id", None)`
- `self.kind = kind or ("plan" if self.tool_name == "ExitPlanMode" else "permission")`
- `self.questions = questions or []`
- `self.answers = None`

`public()` 增加 `questions`（不包含答案）。

`respond_interaction` 在 `resolve` 前增加 choice 校验：

```python
answers = body.get("answers")
if inter.kind == "choice" and behavior == "allow":
    if not isinstance(answers, dict):
        return 400, {"error": "choice 必须提供 answers"}
    normalized = {}
    for q in inter.questions:
        text = q.get("question")
        answer = answers.get(text)
        if not isinstance(answer, str) or not answer.strip():
            return 400, {"error": f"缺少问题答案: {text}"}
        normalized[text] = answer.strip()
    inter.answers = normalized
```

- [ ] **Step 2: 测试**

```python
    def test_choice_requires_all_answers(self):
        hosted = type("H", (), {"hid": None, "session_id": "sid-1"})()
        inter = monitor.Interaction("c1", hosted, {"tool_name": "AskUserQuestion"},
                                    kind="choice",
                                    questions=[{"question": "Q1"}])
        self.mon.interactions["c1"] = inter
        code, _ = self.mon.respond_interaction("c1", {"behavior": "allow"})
        self.assertEqual(code, 400)
        code, body = self.mon.respond_interaction(
            "c1", {"behavior": "allow", "answers": {"Q1": "A"}})
        self.assertEqual(code, 200)
        self.assertEqual(inter.answers, {"Q1": "A"})
```

- [ ] **Step 3: 运行并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api -v
git add monitor.py tests/test_management_api.py
git commit -m "feat: AskUserQuestion 交互模型"
```

---

### Task 8: hook 子命令与 `/hook/PreToolUse`

**Files:**
- Modify: `monitor.py`（`main`、`Hosted.start`、`make_handler`、`Monitor`）
- Test: `tests/test_management_api.py`、`tests/test_interactions.py`

- [ ] **Step 1: 实现 hook 客户端**

顶部补 `import sys`、`import hmac`。

`main` 开头：

```python
if len(sys.argv) > 1 and sys.argv[1] == "hook":
    return hook_client(sys.argv[2:])
```

`hook_client`：

```python
def hook_client(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--event", default="PreToolUse")
    args = ap.parse_args(argv)
    payload = sys.stdin.read()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{args.port}/hook/{args.event}",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Monitor-Token": args.token})
        with urllib.request.urlopen(req, timeout=3600) as resp:
            sys.stdout.write(resp.read().decode("utf-8"))
    except Exception:
        sys.stdout.write("{}")
    return 0
```

`main()` 末尾 `return 0`，并在 `if __name__ == "__main__": sys.exit(main())` 中返回。

- [ ] **Step 2: 托管注册 settings**

`Monitor.__init__` 增加：

```python
self.http_port = 0
self.hook_token = ""
self.hook_interactions = {}
```

`Monitor` 方法：

```python
def hook_command(self):
    args = [sys.executable, str(Path(__file__).resolve()), "hook",
            "--port", str(self.http_port), "--token", self.hook_token]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
```

`Hosted.start` 在基础参数后：

```python
if self.mon.http_port and self.mon.hook_token \
        and not _arg_present(self.mon.claude_args, "--settings"):
    settings = {"hooks": {"PreToolUse": [{
        "matcher": "AskUserQuestion",
        "hooks": [{"type": "command", "command": self.mon.hook_command(),
                   "timeout": 604800}]}]}}
    cmd += ["--settings", json.dumps(settings, separators=(",", ":"))]
elif _arg_present(self.mon.claude_args, "--settings"):
    print(f"[hosted] 检测到 --settings，跳过 AskUserQuestion hook 注册", flush=True)
```

`main` 创建 server 后：

```python
mon.http_port = server.server_port
mon.hook_token = secrets.token_hex(16)
```

- [ ] **Step 3: 服务端 hook 端点**

`do_POST` 最前面：

```python
if u.path.startswith("/hook/"):
    return self._handle_hook(u.path[len("/hook/"):])
```

`_handle_hook`：

```python
def _handle_hook(self, event):
    peer = self.client_address[0]
    if peer not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return self._json({"error": "forbidden"}, 403)
    token = self.headers.get("X-Monitor-Token", "")
    if not mon.hook_token or not hmac.compare_digest(token, mon.hook_token):
        return self._json({"error": "unauthorized"}, 401)
    payload = self._read_body()
    if not isinstance(payload, dict):
        return self._json({})
    if event != "PreToolUse" or payload.get("tool_name") != "AskUserQuestion":
        return self._json({})
    sid = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    tool_input = payload.get("tool_input") or {}
    questions = tool_input.get("questions") or []
    key = f"{sid}\x00{tool_use_id}"
    with mon.lock:
        h = mon.hosted_by_sid.get(sid)
    with mon.lock:
        inter = mon.hook_interactions.get(key)
        if inter is None or inter.state != "pending":
            inter = Interaction("hook-" + secrets.token_hex(8), h,
                                {"tool_name": "AskUserQuestion", "tool_use_id": tool_use_id,
                                 "input": tool_input},
                                session_id=sid, kind="choice", questions=questions)
            mon.interactions[inter.id] = inter
            mon.hook_interactions[key] = inter
            emit = True
        else:
            emit = False
    if emit:
        sess = mon.by_sid(sid)
        if sess:
            mon.emit(sess, "interaction_pending", inter.public())
    timeout = mon.permission_timeout if mon.permission_timeout > 0 else 3600
    inter.event.wait(timeout)
    if inter.state == "pending":
        inter.resolve("deny", reason="审批超时，已自动跳过", state="timed_out")
    with mon.lock:
        mon.interactions.pop(inter.id, None)
        mon.hook_interactions.pop(key, None)
    sess = mon.by_sid(sid)
    if sess:
        mon.emit(sess, "interaction_resolved", inter.public())
    if inter.behavior == "allow" and inter.answers:
        updated = dict(tool_input)
        updated["answers"] = inter.answers
        return self._json({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "permissionDecisionReason": "", "updatedInput": updated}})
    return self._json({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": inter.message or "User skipped the question"}})
```

注意：`mon.by_sid(sid)` 在锁外调用；`emit` 内部不持锁。

- [ ] **Step 4: 测试**

```python
    def test_hook_requires_loopback_and_token(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/hook/PreToolUse", {}, {"Content-Type": "application/json"})
        self.assertEqual(error.exception.code, 401)

    def test_hook_returns_choice_and_answers(self):
        self.mon.hook_token = "tok"
        payload = {"session_id": "sid-1", "tool_use_id": "t1",
                   "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": [{"question": "Q1",
                                                 "options": [{"label": "A"}]}]}}
        result = {}
        def call():
            result["resp"] = json.load(self.request(
                "/hook/PreToolUse", payload,
                {"Content-Type": "application/json", "X-Monitor-Token": "tok"}))
        t = threading.Thread(target=call)
        t.start()
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("sid-1")))
        inter = self.mon.pending_interactions("sid-1")[0]
        self.assertEqual(inter["kind"], "choice")
        self.mon.respond_interaction(inter["id"],
                                     {"behavior": "allow", "answers": {"Q1": "A"}})
        t.join(timeout=5)
        self.assertEqual(result["resp"]["hookSpecificOutput"]["updatedInput"]["answers"],
                         {"Q1": "A"})
```

注意：`test_management_api` 的 `self.mon` 没有 `hook_token` 默认值（`""`），设置后再请求。

- [ ] **Step 5: 运行并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api -v
git add monitor.py tests/test_management_api.py
git commit -m "feat: AskUserQuestion PreToolUse hook"
```

---

### Task 9: 前端 Plan / Choice 卡片

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 渲染**

`interactionsHtml` 增加：

```js
if(it.kind === "plan"){
  return `<div class="inter-card" data-iid="${esc(it.id)}">
    <div><b>计划待确认</b></div>
    <pre class="tool-out">${esc((it.preview || {}).text || "")}</pre>
    <div style="margin-top:6px;display:flex;gap:8px">
      <button class="back" data-act="allow" data-scope="once">批准计划</button>
      <button class="back" data-act="deny"
        data-reason="The user wants to keep planning.">继续规划</button>
    </div></div>`;
}
if(it.kind === "choice"){
  return `<div class="inter-card" data-iid="${esc(it.id)}" data-choice="1">
    <div><b>需要你的选择</b></div>
    ${(it.questions || []).map((q, qi) => `
      <div class="choice-q" data-qi="${qi}" data-q="${esc(q.question)}">
        <div><b>${esc(q.question)}</b></div>
        ${(q.options || []).map((o, oi) => `
          <label class="ctl"><input type="${q.multiSelect ? "checkbox" : "radio"}"
            name="q${qi}" data-label="${esc(o.label)}"> ${esc(o.label)}</label>`).join("")}
        <input class="choice-free" placeholder="其他答案（可选）" maxlength="2000">
      </div>`).join("")}
    <div style="margin-top:6px;display:flex;gap:8px">
      <button class="back" data-act="choice-submit">提交</button>
      <button class="back" data-act="deny">跳过</button>
    </div></div>`;
}
```

- [ ] **Step 2: 提交逻辑**

```js
function collectChoiceAnswers(card){
  const answers = {};
  for(const q of card.querySelectorAll(".choice-q")){
    const text = q.dataset.q;
    const free = q.querySelector(".choice-free").value.trim();
    const picked = [...q.querySelectorAll("input:checked")].map(x => x.dataset.label);
    if(free) answers[text] = free;
    else if(picked.length) answers[text] = picked.join(", ");
  }
  return answers;
}
```

`respondInteraction(card, act, scope, reason)` 扩展：

```js
let body;
if(act === "choice-submit"){
  const answers = collectChoiceAnswers(card);
  if(Object.keys(answers).length !== card.querySelectorAll(".choice-q").length){
    card.insertAdjacentHTML("beforeend",
      '<div class="terror">请回答所有问题或点击跳过</div>');
    return;
  }
  body = {behavior: "allow", answers};
} else if(act === "deny"){
  body = {behavior: "deny", reason: reason || undefined};
} else {
  body = {behavior: act, scope};
}
```

事件委托：

```js
const act = e.target.closest && e.target.closest("#interactions button[data-act]");
if(act){
  respondInteraction(act.closest(".inter-card"), act.dataset.act,
                     act.dataset.scope || "once", act.dataset.reason);
  return;
}
```

- [ ] **Step 3: 测试**

- choice 卡片 HTML 包含问题与选项；
- `collectChoiceAnswers` 单选、多选、自由文本优先级；
- plan 卡片包含两个按钮与固定 reason。

- [ ] **Step 4: 运行并提交**

```bash
node --test tests/test_frontend_controls.js
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 前端计划与选择卡片"
```

---

### Task 10: 全量验证与真实 hook 冒烟

- [ ] **Step 1: 全部测试**

```bash
uv run python -m py_compile monitor.py tests/smoke_permission_live.py
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```

- [ ] **Step 2: 服务冒烟**

```bash
uv run monitor.py --port 8799 --interval 2
```

确认：
- `/events` 首帧是 `snapshot`；
- 新消息触发前端刷新；
- 托管会话的流式文本出现后消失；
- plan/choice 卡片可提交。

- [ ] **Step 3: 真实 hook 启动冒烟（不依赖模型）**

启动一个带 `--settings` 的托管进程并确认 `system/init` 正常返回；可用 `monitor.py` 的 `/hosted` 接口或现有 fake 路径。真实 AskUserQuestion 需要模型服务可用。

- [ ] **Step 4: 更新 CLAUDE.md**

补充：
- SSE 快照/`turn_delta`；
- 消息分页参数；
- `monitor.py hook` 子命令与 `/hook/PreToolUse`；
- plan/choice 卡片。

- [ ] **Step 5: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: SSE、分页与网页问答说明"
```