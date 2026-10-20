# 子代理运行状态与 /command 即时反馈实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 看板标识哪些 subagent 正在运行（卡片 + 折叠区，总数/运行数/逐条状态），网页发送 `/command` 后有本地回显、命令气泡与"正在执行"提示。

**Architecture:** 后端在现有 `subagents` 条目上补 `tid`/`state`（配对逻辑复用 `active_agents` 的 tool_use→tool_result 机制），`parse_msg_line` 把 `<command-name>`/`<local-command-stdout>` 系统行解析成新 part 类型；前端在现有渲染/发送路径上加状态圆点、命令气泡与 pendingEcho 回显，不新增接口或连接。

**Tech Stack:** Python 标准库（unittest）、自包含 Jinja SPA（templates/index.html，node --test vm 测试）。

**规格:** `docs/superpowers/specs/2026-10-20-subagent-status-command-feedback-design.md`

**测试命令:**

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v   # 后端
node --test tests/test_frontend_controls.js                                  # 前端
```

---

### Task 0: 补提交设计文档

**Files:**
- 已写好: `docs/superpowers/specs/2026-10-20-subagent-status-command-feedback-design.md`

- [ ] **Step 1: 提交（若尚未提交）**

先 `git status --short` 确认；若 spec 文件未提交：

```bash
git add docs/superpowers/specs/2026-10-20-subagent-status-command-feedback-design.md
git commit -m "docs: 子代理运行状态与命令即时反馈设计

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 1: 后端 — subagents 条目状态流转与输出

**Files:**
- Modify: `monitor.py`（`_on_tool_use` 约 948-955 行、`_on_tool_result` 约 974-979 行、`summary` 约 331 行、`detail` 约 340 行）
- Test: `tests/test_session_state.py`（沿用现有 `SessionStateTests` 的 setUp/line 辅助）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_session_state.py` 的 `SessionStateTests` 类内：

```python
    def test_subagent_entry_running_then_done(self):
        self.line("assistant", [{
            "type": "tool_use", "id": "a1", "name": "Agent",
            "input": {"description": "scan code", "subagent_type": "Explore"},
        }], stop_reason="tool_use")
        sub = self.sess.subagents[0]
        self.assertEqual(sub["tid"], "a1")
        self.assertEqual(sub["state"], "running")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 1)

        self.line("user", [{"type": "tool_result", "tool_use_id": "a1", "content": "ok"}])
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["subagents_running"], 0)

    def test_subagent_entry_error_on_error_result(self):
        self.line("assistant", [{
            "type": "tool_use", "id": "a1", "name": "Agent",
            "input": {"description": "scan"},
        }], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1",
                            "content": "boom", "is_error": True}])
        self.assertEqual(self.sess.subagents[0]["state"], "error")

    def test_detail_orders_running_subagents_first(self):
        self.line("assistant", [{"type": "tool_use", "id": "a1", "name": "Agent",
                                 "input": {"description": "first"}}], stop_reason="tool_use")
        self.line("user", [{"type": "tool_result", "tool_use_id": "a1", "content": "ok"}])
        self.line("assistant", [{"type": "tool_use", "id": "a2", "name": "Agent",
                                 "input": {"description": "second"}}], stop_reason="tool_use")
        subs = self.sess.detail(self.mon.stale_after)["subagents"]
        self.assertEqual([s["description"] for s in subs], ["second", "first"])
```

- [ ] **Step 2: 运行验证失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_state -v
```

预期：新增 3 个测试 FAIL（`KeyError: 'tid'` / `KeyError: 'state'` / `KeyError: 'subagents_running'`），原有测试 PASS。

- [ ] **Step 3: 实现**

`monitor.py` `_on_tool_use` 的 Agent 分支（当前 948-955 行）改为：

```python
        if name == "Agent":
            if tid:
                sess.active_agents.add(tid)
            sub = {"type": inp.get("subagent_type") or "general-purpose",
                   "description": clip(inp.get("description", ""), 120),
                   "ts": now_ts(),
                   "tid": tid or "",
                   # 无 tid 无法配对结果，直接视为已完成
                   "state": "running" if tid else "done"}
            sess.subagents.append(sub)
            self.emit(sess, "subagent_start", sub)
```

`_on_tool_result`（当前 974-979 行）改为：

```python
    def _on_tool_result(self, sess, b):
        tid = b.get("tool_use_id")
        sess.active_agents.discard(tid)
        sess.pending_tools.discard(tid)
        if b.get("is_error"):
            self.emit(sess, "tool_error", {"tool_use_id": tid,
                                           "error": clip(b.get("content", ""), 200)})
        for sub in sess.subagents:
            if sub.get("tid") == tid and sub.get("state") == "running":
                sub["state"] = "error" if b.get("is_error") else "done"
                self.emit(sess, "subagent_done",
                          {"tid": tid, "state": sub["state"],
                           "type": sub.get("type"),
                           "description": sub.get("description")})
                break
```

`summary()` 里 `"subagent_count": len(self.subagents),` 之后加一行：

```python
            "subagents_running": sum(1 for a in self.subagents
                                     if a.get("state") == "running"),
```

`detail()` 的 subagents 排序（当前 340 行）改为运行中在前、其余按 ts 倒序：

```python
        d["subagents"] = sorted(self.subagents,
                                key=lambda sub: (sub.get("state") != "running",
                                                 -sub.get("ts", 0)))
```

- [ ] **Step 4: 运行验证通过**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_state -v
```

预期：全部 PASS（原有 active_agents 状态机测试不受影响）。

- [ ] **Step 5: 提交**

```bash
git add monitor.py tests/test_session_state.py
git commit -m "feat: 子代理条目标记运行状态并输出运行计数

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 2: 后端 — 扫描缓存兼容旧条目

**Files:**
- Modify: `monitor.py`（`restore_cache` 约 284-286 行）
- Test: `tests/test_session_state.py`

- [ ] **Step 1: 写失败测试**

追加到 `SessionStateTests`：

```python
    def test_restore_old_subagent_entries_marked_done(self):
        self.sess.restore_cache({
            "subagents": [{"type": "Explore", "description": "d", "ts": 1},
                          {"type": "claude", "description": "e", "ts": 2,
                           "tid": "t2", "state": "running"}],
        })
        self.assertEqual(self.sess.subagents[0]["state"], "done")
        self.assertEqual(self.sess.subagents[0]["tid"], "")
        # 新格式条目原样保留（运行中的子任务在增量读取中会等来 tool_result）
        self.assertEqual(self.sess.subagents[1]["state"], "running")
        self.assertEqual(self.sess.subagents[1]["tid"], "t2")
```

- [ ] **Step 2: 运行验证失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_state -v
```

预期：新测试 FAIL（`KeyError: 'state'` / `KeyError: 'tid'`）。

- [ ] **Step 3: 实现**

`restore_cache` 中（当前 284-286 行）：

```python
        subagents = data.get("subagents")
        if isinstance(subagents, list):
            self.subagents = [x for x in subagents if isinstance(x, dict)]
            # 旧缓存条目没有 tid/state：无法继续配对，一律视为已完成
            for sub in self.subagents:
                if not isinstance(sub.get("state"), str):
                    sub["state"] = "done"
                if not isinstance(sub.get("tid"), str):
                    sub["tid"] = ""
```

- [ ] **Step 4: 运行验证通过**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_state tests.test_scan_cache -v
```

预期：全部 PASS（`cache_state` 对新字段自动生效——`copy.deepcopy(self.subagents)` 已包含）。

- [ ] **Step 5: 提交**

```bash
git add monitor.py tests/test_session_state.py
git commit -m "feat: 扫描缓存兼容子代理状态字段

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 3: 后端 — 命令行解析为 command/command_output part

**Files:**
- Modify: `monitor.py`（`parse_msg_line` 约 2066-2113 行；`re` 已在文件头部导入）
- Test: `tests/test_transcript_parsing.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_transcript_parsing.py` 的 `ParseMessageTests` 类内：

```python
    def test_command_line_parses_to_command_part(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content":
                        "<command-name>/model</command-name>\n"
                        "            <command-message>model</command-message>\n"
                        "            <command-args>deepseek-v4.1-flash</command-args>"},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["role"], "system")
        self.assertEqual(len(msg["parts"]), 1)
        self.assertEqual(msg["parts"][0]["type"], "command")
        self.assertEqual(msg["parts"][0]["command"], "/model")
        self.assertEqual(msg["parts"][0]["args"], "deepseek-v4.1-flash")

    def test_command_line_without_args(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content":
                        "<command-name>/compact</command-name>\n"
                        "            <command-message>compact</command-message>"},
        }
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertEqual(part["command"], "/compact")
        self.assertEqual(part["args"], "")

    def test_local_command_stdout_parses_to_output_part(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content":
                        "<local-command-stdout>switched to model x</local-command-stdout>"},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["role"], "system")
        self.assertEqual(msg["parts"][0]["type"], "command_output")
        self.assertEqual(msg["parts"][0]["text"], "switched to model x")

    def test_other_system_text_unchanged(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["parts"][0]["type"], "text")
```

- [ ] **Step 2: 运行验证失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_transcript_parsing -v
```

预期：前 3 个新测试 FAIL（parts 里是普通 text part），第 4 个 PASS。

- [ ] **Step 3: 实现**

`monitor.py` 模块级（`SYSTEM_MARKERS` 定义之后、`classify_user_line` 之前）加正则：

```python
COMMAND_NAME_RE = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
LOCAL_STDOUT_RE = re.compile(r"^<local-command-stdout>\s*(.*?)\s*</local-command-stdout>$", re.S)
```

`parse_msg_line` 的 text part 分支（当前 2097-2105 行）改为：

```python
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and (b.get("text") or "").strip():
            text = b["text"]
            stripped = text.lstrip()
            if kind == "system" and stripped.startswith("<command-name>"):
                m = COMMAND_NAME_RE.search(text)
                if m:
                    am = COMMAND_ARGS_RE.search(text)
                    parts.append({"type": "command",
                                  "command": clip(m.group(1), 120),
                                  "args": clip(am.group(1).strip(), 200) if am else ""})
                    continue
            if kind == "system":
                lm = LOCAL_STDOUT_RE.match(stripped)
                if lm:
                    parts.append({"type": "command_output",
                                  "text": clip(lm.group(1), 2000)})
                    continue
            if kind == "meta_tool":
                parts.append({"type": "tool_note",
                              "tool_use_id": o.get("sourceToolUseID"),
                              "text": clip(text, 3000)})
            else:
                # 系统文本（任务通知/命令输出等）截短即可，不需要全文
                parts.append({"type": "text",
                              "text": clip(text, 500 if role == "system" else clip_len)})
```

（`<command-name>` 行解析失败时落到普通 text 分支，保持旧行为兜底。）

- [ ] **Step 4: 运行验证通过**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_transcript_parsing tests.test_session_state -v
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add monitor.py tests/test_transcript_parsing.py
git commit -m "feat: 转录命令行解析为命令气泡 part

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 4: 前端 — 子代理状态展示（卡片 chip + 折叠区 + SSE 注册）

**Files:**
- Modify: `templates/index.html`（SSE_TYPES 约 561 行、卡片 chip 约 778 行、折叠区约 799-800 行、`<style>` 块）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_frontend_controls.js` 末尾：

```javascript
test('子代理折叠区显示运行数与状态圆点', () => {
  const b = boot();
  const html = b.run(`infoHtml({id:"s1", tokens:{}, subagents:[
    {type:"Explore", description:"scan", state:"running"},
    {type:"claude", description:"write", state:"done"},
    {type:"claude", description:"bad", state:"error"},
  ], tasks:[]})`);
  assert.ok(html.includes('1 运行中'));
  assert.ok(html.includes('dot run'));
  assert.ok(html.includes('dot done'));
  assert.ok(html.includes('dot err'));
});

test('会话卡片 chip 显示运行中子代理数', () => {
  const b = boot();
  b.run(`renderSessionList({cwd:"/p", sessions:[
    {id:"s1", state:"idle", subagent_count:3, subagents_running:2},
    {id:"s2", state:"idle", subagent_count:1, subagents_running:0},
  ], out:0})`);
  const html = b.node("main").innerHTML;
  assert.ok(html.includes('3 子代理 · 2 运行中'));
  assert.ok(html.includes('1 子代理'));
  assert.ok(!html.includes('1 子代理 ·'));   // 无运行中时不追加
});
```

- [ ] **Step 2: 运行验证失败**

```bash
node --test tests/test_frontend_controls.js
```

预期：2 个新测试 FAIL（无 "运行中" 文案、无 dot class）。

- [ ] **Step 3: 实现**

1. `SSE_TYPES`（约 561 行）数组中 `"subagent_start",` 后插入 `"subagent_done",`。

2. 卡片 chip（约 778 行）：

```javascript
          s.subagent_count && `${s.subagent_count} 子代理${s.subagents_running ? ` · ${s.subagents_running} 运行中` : ""}`,
```

3. 折叠区（约 799-800 行）改为：

```javascript
    ${d.subagents && d.subagents.length ? (() => {
      const run = d.subagents.filter(a => a.state === "running").length;
      const dot = st => st === "running" ? '<span class="dot run" title="运行中"></span>'
        : st === "error" ? '<span class="dot err" title="出错"></span>'
        : '<span class="dot done" title="已完成"></span>';
      return `<details class="fold"${detailsAttrs(`info:${sid}:subagents`)}><summary>子代理 (${d.subagents.length})${run ? ` · ${run} 运行中` : ""}</summary><ul>` +
        d.subagents.map(a => `<li>${dot(a.state)}<span class="chip">${esc(a.type)}</span> ${esc(a.description)}</li>`).join("") + "</ul></details>";
    })() : ""}
```

（服务端 `detail()` 已按运行中在前排序，前端按返回顺序渲染。）

4. `<style>` 块追加（放在现有 `.chip.pending` 规则附近）：

```css
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.run{background:#f59e0b;animation:dotpulse 1.2s ease-in-out infinite}
.dot.done{background:#9ca3af}
.dot.err{background:#ef4444}
@keyframes dotpulse{0%,100%{opacity:1}50%{opacity:.35}}
```

- [ ] **Step 4: 运行验证通过**

```bash
node --test tests/test_frontend_controls.js
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 看板展示子代理运行状态

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 5: 前端 — 命令气泡与命令状态文案

**Files:**
- Modify: `templates/index.html`（`convHtml` 约 831-852 行、`sendJobText` 约 1132-1144 行、`bindSendBox`/`bindNewBox` 的 sendJobs 赋值、`<style>`）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 写失败测试**

```javascript
test('command part 渲染为命令气泡与折叠输出', () => {
  const b = boot();
  const html = b.run(`convHtml([{role:"system", ts:0, parts:[
    {type:"command", command:"/model", args:"deepseek-v4.1-flash"},
    {type:"command_output", text:"switched"},
  ]}])`);
  assert.ok(html.includes('class="cmd"'));
  assert.ok(html.includes('/model'));
  assert.ok(html.includes('deepseek-v4.1-flash'));
  assert.ok(html.includes('命令输出'));
  assert.ok(html.includes('switched'));
});

test('sendJobText 对斜杠命令显示命令文案', () => {
  const b = boot();
  b.run(`sendJobs["s1"] = {status:"sending", text:"/model deepseek"}`);
  assert.equal(b.run(`sendJobText("s1")`), "命令 /model 发送中…");
  b.run(`sendJobs["s1"] = {status:"queued", text:"/model deepseek"}`);
  assert.equal(b.run(`sendJobText("s1")`), "命令 /model 排队中…");
  b.run(`sendJobs["s1"] = {status:"running", text:"/model deepseek"}`);
  assert.equal(b.run(`sendJobText("s1")`), "正在执行命令 /model…");
  b.run(`sendJobs["s1"] = {status:"running", text:"hello"}`);
  assert.equal(b.run(`sendJobText("s1")`), "Claude 正在处理…");
});
```

- [ ] **Step 2: 运行验证失败**

```bash
node --test tests/test_frontend_controls.js
```

预期：2 个新测试 FAIL。

- [ ] **Step 3: 实现**

1. `convHtml` 的 part 循环里、`if(p.type === "text")` 分支之后加：

```javascript
      if(p.type === "command")
        return `<div class="cmd">⌘ <code>${esc(p.command)}</code>${p.args ? " " + esc(p.args) : ""}</div>`;
      if(p.type === "command_output")
        return `<details class="fold"><summary>命令输出</summary><pre class="tool-out">${esc(p.text)}</pre></details>`;
```

2. `sendJobText` 改为：

```javascript
function sendJobText(id){
  // 状态从模块级 sendJobs 读取，而不是闭包里的 DOM 引用——
  // 自动刷新会整体重渲染对话视图，旧引用会指向已替换的节点导致界面停在"发送中…"
  const j = sendJobs[id];
  if(!j) return "";
  const cmd = typeof j.text === "string" && j.text.trim().startsWith("/")
    ? "命令 " + j.text.trim().split(/\s+/)[0] : "";
  if(j.status === "sending") return cmd ? `${cmd} 发送中…` : "发送中…";
  if(j.status === "queued") return cmd ? `${cmd} 排队中…` : "排队中…";
  if(j.status === "running") return cmd ? `正在执行${cmd}…` : "Claude 正在处理…";
  if(j.status === "done") return "已完成，回复见下方对话";
  if(j.status === "cancelled") return "已中断";
  if(j.status === "failed") return "失败: " + (j.error || "未知错误");
  return "";
}
```

3. 发送时把原文记进 sendJobs（轮询更新时保留）：

- `bindNewBox`：`sendJobs[key] = {status: "sending", requested_model: selectedModel};`
  改为 `sendJobs[key] = {status: "sending", requested_model: selectedModel, text};`
- `bindSendBox`：`sendJobs[id] = {status: "sending", requested_model: selectedModel};`
  改为 `sendJobs[id] = {status: "sending", requested_model: selectedModel, text};`
- 两处轮询循环里的状态覆盖（`bindNewBox` 约 1260 行、`bindSendBox` 约 1363 行）都补上
  `text: (sendJobs[sid || id] || {}).text` / `text: (sendJobs[id] || {}).text`，例如：

```javascript
        sendJobs[id] = {jobId: j.job_id, status: jr.status,
                        result: jr.result, error: jr.error,
                        text: (sendJobs[id] || {}).text};
```

4. `<style>` 追加：

```css
.cmd{background:var(--card2);border:1px solid var(--bd);border-radius:6px;padding:4px 10px;font-size:12.5px;display:inline-block;margin:2px 0}
```

- [ ] **Step 4: 运行验证通过**

```bash
node --test tests/test_frontend_controls.js
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 命令气泡渲染与命令执行状态文案

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 6: 前端 — 本地回显（pendingEcho）

**Files:**
- Modify: `templates/index.html`（模块级状态区约 222 行、`renderConvList` 约 1030-1046 行、`bindSendBox`/`bindNewBox` 发送路径、`clearSessionView` 约 1393 行、`<style>`）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 写失败测试**

```javascript
test('echoVisible 按文本对账', () => {
  const b = boot();
  assert.equal(b.run(`echoVisible([], {text:" hi "})`), true);
  assert.equal(b.run(`echoVisible([{role:"user", parts:[{type:"text", text:"hi"}]}], {text:"hi"})`), false);
  assert.equal(b.run(`echoVisible([{role:"assistant", parts:[{type:"text", text:"hi"}]}], {text:"hi"})`), true);
  assert.equal(b.run(`echoVisible(null, {text:"hi"})`), false);
  assert.equal(b.run(`echoVisible([], null)`), false);
});

test('发送 202 后 pendingEcho 记录', async () => {
  const b = boot(async(url) => {
    if(url === "/sessions/sid/message") return {ok:true, status:202, json:async()=>({job_id:"j1"})};
    if(url === "/jobs/j1") return response({status:"done"});
    return response({});
  });
  b.run('bindSendBox("sid", {})');
  b.node("msgbox").value = "hello";
  b.node("sendbtn").click();
  for(let i = 0; i < 30; i++) await Promise.resolve();
  assert.deepEqual(b.run("pendingEcho['sid']"), {text:"hello"});
});
```

注意：`echoVisible` 的可见性在渲染时使用；第二个测试只验证记录与清空逻辑。若实现里
202 后赋值 `{text}` 之外还带其他字段，调整断言为 `assert.equal(b.run("pendingEcho['sid']?.text"), "hello")`。

- [ ] **Step 2: 运行验证失败**

```bash
node --test tests/test_frontend_controls.js
```

预期：新测试 FAIL（`echoVisible is not defined` / `pendingEcho` 未定义）。

- [ ] **Step 3: 实现**

1. 模块级状态区（`let sendJobs = {};` 附近）加：

```javascript
let pendingEcho = {};            // sid -> {text, failed} 发送后未落盘的本地回显
```

2. 纯函数（放在 `sendJobText` 附近）：

```javascript
function echoVisible(messages, echo){
  // 转录里出现同文本 user 消息后回显退场；只查最近 20 条，避免长对话全量比对
  if(!echo || !echo.text) return false;
  const t = echo.text.trim();
  for(let i = messages.length - 1; i >= 0 && i >= messages.length - 20; i--){
    const m = messages[i];
    if(m.role !== "user") continue;
    for(const p of m.parts || [])
      if(p.type === "text" && (p.text || "").trim() === t) return false;
  }
  return true;
}
function echoHtml(echo){
  return `<div class="msg user pending${echo.failed ? " failed" : ""}">` +
    `<div class="mhead">用户 · ${echo.failed ? "发送失败" : "发送中…"}</div>` +
    `<div class="ubody">${esc(echo.text)}</div></div>`;
}
```

3. `renderConvList` 开头改为（回显插在顶部，转录出现同文本后清除；其余行保持原样）：

```javascript
function renderConvList(){
  const el = document.getElementById("conv");
  if(!el) return;
  const echo = pendingEcho[convState.id];
  if(echo && !echoVisible(convState.msgs, echo)) delete pendingEcho[convState.id];
  const echoOut = pendingEcho[convState.id] ? echoHtml(pendingEcho[convState.id]) : "";
  el.innerHTML = echoOut + convHtml(convState.msgs.slice().reverse()) +
    (convState.hasMore
      ? '<div style="text-align:center;margin:10px 0">' +
        '<button class="back" id="loadearlier">加载更早</button></div>'
      : "");
  const btn = document.getElementById("loadearlier");
  if(btn) btn.onclick = loadEarlier;
  const count = document.getElementById("convcount");
  if(count) count.textContent = `对话 (${convState.msgs.length}/${convState.total})`;
  if(!liveText){
    const live = document.getElementById("livebubble");
    if(live && live.remove) live.remove();
  }
}
```

4. `bindSendBox`：拿到 202 后、`load()` 之前加：

```javascript
      pendingEcho[id] = {text};
      renderConvList();
```

失败路径（非 202 与 catch、等待超时）加：

```javascript
      if(pendingEcho[id]) pendingEcho[id].failed = true;
```

5. `bindNewBox`：`enterSession()` 里 `sendJobs[sid] = sendJobs[key] ...` 之后加：

```javascript
      pendingEcho[sid] = {text};
```

失败路径（进程退出、超时、catch）同样在对应 `sendJobs` 赋 failed 处标记
`if(sid && pendingEcho[sid]) pendingEcho[sid].failed = true;`（`sid` 为 null 时
新建面板无对话区，无需回显）。

6. `clearSessionView` 不清 `pendingEcho`（按 sid 键控，切回时仍有效，转录落盘后自然消失）。

7. `<style>` 追加：

```css
.msg.user.pending{opacity:.55}
.msg.user.pending .mhead{font-weight:600}
.msg.user.pending.failed{opacity:1}
.msg.user.pending.failed .mhead{color:#ef4444}
```

- [ ] **Step 4: 运行验证通过**

```bash
node --test tests/test_frontend_controls.js
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 发送消息本地回显

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

### Task 7: 全量回归 + CLAUDE.md 更新

**Files:**
- Modify: `CLAUDE.md`（"数据流"第 2 条、"前端"段落）

- [ ] **Step 1: 全量测试**

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```

预期：全部 PASS。真实 CLI 冒烟（可选，会调用模型）：
`CLAUDE_SESSIONS_SMOKE=1 uv run python tests/smoke_permission_live.py`。

- [ ] **Step 2: 更新 CLAUDE.md**

"数据流"第 2 条 `handle_line` 描述末尾加一句：

```
Agent 工具调用的子代理条目带 tid/state（running/done/error），tool_result 回传时
翻转状态并发 `subagent_done`；`<command-name>`/`<local-command-stdout>` 系统行
解析为 command/command_output part。
```

"前端"段落加一句：

```
子代理折叠区与卡片显示运行中数量（脉动圆点）；发送后有本地回显气泡（转录落盘
同文本后消失），斜杠命令渲染为命令气泡并在状态行显示"正在执行命令 /xxx…"。
```

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: 记录子代理状态与命令反馈行为

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```
