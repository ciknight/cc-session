# 托管审批、中断、排队与回合判定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `monitor.py` 的常驻 stream-json 托管进程补齐权限审批、中断与可靠排队，并用双时钟 + 显式回合结束标记改进监控状态，同时增强转录解析。

**Architecture:** 保持单文件 Python 服务。`Hosted` 负责 stream-json 双向协议（`can_use_tool`、`command_lifecycle`、`interrupt`）；`Monitor` 维护审批交互表与转录解析；`Session` 增加双时钟、pending tool、context 用量和子代理字段；HTTP 层新增交互/中断/审批模式端点；Jinja 前端增加审批卡片、中断按钮和富工具结果展示。

**Tech Stack:** Python 3.9+ 标准库、Jinja2、原生 JS、unittest。

**Spec:** `docs/superpowers/specs/2026-10-19-hosted-approval-turn-detection-design.md`

**测试命令（每个任务结束都运行）：**

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
```

---

### Task 1: 审批模式持久化与 Monitor 接线

**Files:**
- Modify: `session_preferences.py`
- Modify: `monitor.py`（`Monitor.__init__`、`main()`）
- Test: `tests/test_session_preferences.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_session_preferences.py` 末尾加入：

```python
from session_preferences import ApprovalPreferences


class ApprovalPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "approvals.json"
        self.addCleanup(self.tmp.cleanup)

    def test_default_ask_and_persist(self):
        prefs = ApprovalPreferences(self.path)
        self.assertEqual(prefs.get("sid-1"), "ask")
        prefs.set("sid-1", "full")
        self.assertEqual(ApprovalPreferences(self.path).get("sid-1"), "full")

    def test_invalid_mode_rejected(self):
        prefs = ApprovalPreferences(self.path)
        with self.assertRaises(ValueError):
            prefs.set("sid-1", "bypass")

    def test_corrupt_file_does_not_overwrite(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{broken", encoding="utf-8")
        prefs = ApprovalPreferences(self.path)
        with self.assertRaises(ValueError):
            prefs.set("sid-1", "auto")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_preferences.ApprovalPreferenceTests -v
```

Expected: `ImportError: cannot import name 'ApprovalPreferences'`。

- [ ] **Step 3: 实现 `ApprovalPreferences`**

在 `session_preferences.py` 末尾追加：

```python
APPROVAL_MODES = ("ask", "auto", "full")


class ApprovalPreferences:
    """看板独立的托管审批模式，不修改 Claude 配置或转录。"""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.modes = {}
        self.error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("approvals"), dict)):
                raise ValueError("格式或版本不支持")
            for sid, mode in data["approvals"].items():
                if not isinstance(sid, str) or mode not in APPROVAL_MODES:
                    raise ValueError("审批模式记录无效")
            self.modes = data["approvals"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "审批模式文件无法读取或格式损坏，请先修复或移走该文件"

    def get(self, sid, default="ask"):
        with self.lock:
            return self.modes.get(sid, default)

    def set(self, sid, mode):
        if mode not in APPROVAL_MODES:
            raise ValueError("审批模式必须是 ask / auto / full")
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            updated = dict(self.modes)
            updated[sid] = mode
            temp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temp = tempfile.mkstemp(prefix="approvals-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "approvals": updated}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
            except OSError as exc:
                raise ValueError("审批模式保存失败，请检查目录权限或磁盘空间") from exc
            finally:
                if temp and os.path.exists(temp):
                    os.unlink(temp)
            self.modes = updated
            return mode
```

- [ ] **Step 4: Monitor 接线**

`monitor.py` 顶部导入改为：

```python
from session_preferences import (APPROVAL_MODES, ApprovalPreferences, ModelPreferences,
                                 UNSET, default_model, model_args, validate_model)
```

`Monitor.__init__` 参数增加 `permission_timeout=300`，并加入：

```python
self.permission_timeout = permission_timeout
self.interactions = {}       # request_id -> Interaction
self.approvals = ApprovalPreferences(CLAUDE_DIR / "claude-sessions" / "approvals.json")
```

`main()` 增加参数并传给 Monitor：

```python
ap.add_argument("--permission-timeout", type=int, default=300,
                help="托管会话审批等待秒数，0 表示无限等待")
...
mon = Monitor(args.stale, args.webhook, args.permission_timeout)
```

更新 `Monitor(...)` 的所有测试调用保持不变（`permission_timeout` 有默认值）。

- [ ] **Step 5: 运行全部测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
git add session_preferences.py monitor.py tests/test_session_preferences.py
git commit -m "feat: 持久化托管审批模式"
```

---

### Task 2: 双时钟与显式回合结束

**Files:**
- Modify: `monitor.py`（`Session`、`Monitor.handle_line`）
- Test: `tests/test_session_state.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_session_state.py` 加入：

```python
    def test_turn_duration_wins_over_end_turn(self):
        self.line("user", [{"type": "text", "text": "do it"}])
        self.line("assistant", [{"type": "text", "text": "working"}], stop_reason="end_turn")
        self.assertIn(self.sess.summary(self.mon.stale_after)["state"], ("idle", "working"))
        # end_turn 先到（无 turn_duration 历史）会暂时 idle；随后真实回合结束标记仍应工作
        self.line("system", [], subtype="turn_duration")
        self.assertTrue(self.sess.saw_turn_duration)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_end_turn_is_fallback_without_turn_duration(self):
        self.line("user", [{"type": "text", "text": "hi"}])
        self.line("assistant", [{"type": "text", "text": "done"}], stop_reason="end_turn")
        self.assertFalse(self.sess.saw_turn_duration)
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_tool_result_and_meta_do_not_advance_last_input(self):
        self.line("user", [{"type": "text", "text": "real input"}], timestamp=self.timestamp(self.now))
        self.assertEqual(self.sess.last_input_ts, self.now)
        later = self.now + 10
        self.line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
                  timestamp=self.timestamp(later))
        self.line("user", [{"type": "text", "text": "injected"}], isMeta=True,
                  timestamp=self.timestamp(later + 10))
        self.assertEqual(self.sess.last_input_ts, self.now)

    def test_injected_prompt_does_not_advance_last_input(self):
        self.line("user", [{"type": "text", "text": "injected"}],
                  origin={"kind": "coordinator"}, timestamp=self.timestamp(self.now))
        self.assertIsNone(self.sess.last_input_ts)

    def test_pending_tool_keeps_working_after_end_turn(self):
        self.line("user", [{"type": "text", "text": "go"}])
        self.line("assistant", [{"type": "tool_use", "id": "tool-1", "name": "Bash",
                                 "input": {"command": "sleep 1"}}], stop_reason="tool_use")
        self.line("assistant", [{"type": "text", "text": "waiting"}], stop_reason="end_turn")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "working")
        self.line("user", [{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}])
        self.line("system", [], subtype="turn_duration")
        self.assertEqual(self.sess.summary(self.mon.stale_after)["state"], "idle")

    def test_context_tokens_use_latest_usage_not_sum(self):
        self.line("assistant", [{"type": "text", "text": "a"}], usage={
            "input_tokens": 10, "output_tokens": 2,
            "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4})
        self.assertEqual(self.sess.context_tokens, 19)
        self.line("assistant", [{"type": "text", "text": "b"}], usage={
            "input_tokens": 5, "output_tokens": 1})
        self.assertEqual(self.sess.context_tokens, 6)
        self.assertEqual(self.sess.tokens["input"], 15)
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_state -v
```

Expected: `AttributeError: 'Session' object has no attribute 'last_input_ts'` 等。

- [ ] **Step 3: 实现字段与逻辑**

`Session.__init__` 增加：

```python
self.ai_title = None
self.custom_title = None
self.last_input_ts = None
self.last_turn_end_ts = None
self.saw_turn_duration = False
self.pending_tools = set()
self.context_tokens = 0
self.compacted_at = None
self.is_subagent = False
self.parent_id = None
self.agent_name = None
```

`Session.summary()` 增加字段，并把 stale 判定保持为 `last_event_ts`：

```python
"last_input_at": self.last_input_ts,
"last_turn_end_at": self.last_turn_end_ts,
"context_tokens": self.context_tokens,
"compacted_at": self.compacted_at,
"is_subagent": self.is_subagent,
"parent_id": self.parent_id,
"agent_name": self.agent_name,
```

`Monitor` 增加辅助方法：

```python
def _set_working(self, sess):
    sess.state = "working"

def _set_idle(self, sess):
    if sess.state == "idle":
        return
    sess.state = "idle"
    self.emit(sess, "turn_finished", {"last": sess.last_assistant})
```

`handle_line` 的时间戳更新改为：

```python
ts_epoch = _parse_iso(ts)
if ts_epoch:
    if sess.first_ts is None or ts_epoch < sess.first_ts:
        sess.first_ts = ts_epoch
    sess.last_event_ts = max(sess.last_event_ts, ts_epoch)
elif self.first_scan_done:
    sess.last_event_ts = now_ts()
```

`handle_line` 增加分支（放在 `typ` 分派前）：

```python
if typ == "custom-title":
    value = obj.get("customTitle")
    if value is not None:
        sess.custom_title = value or None
        sess.title = sess.custom_title or sess.ai_title or sess.title
    return
if typ == "system":
    if obj.get("subtype") == "turn_duration":
        sess.saw_turn_duration = True
        sess.last_turn_end_ts = ts_epoch or now_ts()
        if not sess.pending_tools and not sess.active_agents:
            self._set_idle(sess)
    elif obj.get("subtype") == "compact_boundary":
        sess.compacted_at = ts_epoch or now_ts()
        self.emit(sess, "context_compacted", {"at": sess.compacted_at})
    return
```

`ai-title` 分支保留 AI 标题，并让自定义标题优先：

```python
if typ == "ai-title":
    sess.ai_title = obj.get("aiTitle") or sess.ai_title
    sess.title = sess.custom_title or sess.ai_title or sess.title
```

`user` 分支：

- 使用新的 `classify_user_line`（Task 3），先按 `kind` 处理。
- 仅 `kind == "user"` 且非 sidechain 时：`user_msgs += 1`、`last_input_ts = ts_epoch or now_ts()`、`_set_working`、emit。
- `tool_result` 始终调用 `_on_tool_result`。
- `kind in ("injected", "system", "meta_tool")` 时 `_set_working`，但不计入输入。

`assistant` 分支：

```python
usage = msg.get("usage") or {}
sess.tokens[...] += ...
ctx = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
       + usage.get("cache_read_input_tokens", 0) + usage.get("output_tokens", 0))
if ctx > 0:
    sess.context_tokens = ctx
...
if obj.get("isApiErrorMessage"):
    self.emit(sess, "api_error", {"text": clip(_result_text_from_message(msg), 300)})
...
for b in blocks:
    if b.get("type") == "tool_use":
        self._on_tool_use(sess, b, sidechain)
if sidechain:
    self._set_working(sess)
elif not obj.get("isApiErrorMessage"):
    if not sess.saw_turn_duration and msg.get("stop_reason") == "end_turn" and not sess.pending_tools:
        self._set_idle(sess)
```

`_on_tool_use(self, sess, b, sidechain=False)` 增加：

```python
if not sidechain and b.get("id"):
    sess.pending_tools.add(b["id"])
```

`_on_tool_result` 增加：

```python
sess.pending_tools.discard(b.get("tool_use_id"))
```

- [ ] **Step 4: 运行测试并修复**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_state -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add monitor.py tests/test_session_state.py
git commit -m "feat: 双时钟与显式回合结束判定"
```

---

### Task 3: `classify_user_line` 与 isMeta 三分

**Files:**
- Modify: `monitor.py`
- Test: `tests/test_session_state.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_meta_source_tool_is_not_user_input(self):
        self.line("user", [{"type": "text", "text": "skill body"}],
                  isMeta=True, sourceToolUseID="tool-9", timestamp=self.timestamp(self.now))
        self.assertIsNone(self.sess.last_input_ts)
        self.assertEqual(self.sess.user_msgs, 0)

    def test_boilerplate_meta_is_ignored(self):
        self.line("user", [{"type": "text", "text": "system boilerplate"}],
                  isMeta=True, timestamp=self.timestamp(self.now))
        self.assertIsNone(self.sess.last_input_ts)

    def test_interrupt_marker_is_system(self):
        self.line("user", [{"type": "text", "text": "[Request interrupted by user]"}])
        self.assertIsNone(self.sess.last_input_ts)
        self.assertEqual(self.sess.user_msgs, 0)
```

- [ ] **Step 2: 实现分类函数**

替换 `SYSTEM_MARKERS` / `classify_user_line`：

```python
def classify_user_line(o, blocks):
    """返回 user / tool / meta / meta_tool / injected / system。"""
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return "tool"
    if o.get("isCompactSummary"):
        return "system"
    if o.get("isMeta"):
        return "meta_tool" if o.get("sourceToolUseID") else "meta"
    if o.get("origin", {}).get("kind") or o.get("promptSource"):
        return "injected"
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            text = (b.get("text") or "").lstrip()
            if text.startswith(SYSTEM_MARKERS) or text.startswith("[Request interrupted"):
                return "system"
    return "user"
```

`SYSTEM_MARKERS` 移除 `"[Request interrupted"`（已有单独判断，保留也行）。

`handle_line` 的 user 分支按上述 kind 处理；`meta` 直接忽略且不改变状态，`meta_tool/injected/system` 设置 working 但不计输入。

- [ ] **Step 3: 运行并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_session_state -v
git add monitor.py tests/test_session_state.py
git commit -m "feat: 区分 isMeta 与注入输入"
```

---

### Task 4: `toolUseResult` 与消息解析

**Files:**
- Modify: `monitor.py`（`parse_msg_line`、`_result_text`、消息缓存分支）
- Test: `tests/test_transcript_parsing.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_transcript_parsing.py`：

```python
import json
import unittest

import monitor


def line(obj):
    return json.dumps(obj, ensure_ascii=False)


class ParseMessageTests(unittest.TestCase):
    def test_edit_result_uses_structured_patch(self):
        obj = {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                     "content": "File updated"}]},
            "toolUseResult": {"structuredPatch": [
                {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
                 "lines": ["-old", "+new"]}]},
        }
        msg = monitor.parse_msg_line(line(obj))
        part = msg["parts"][0]
        self.assertEqual(part["type"], "tool_result")
        self.assertEqual(part["kind"], "diff")
        self.assertIn("-old", part["text"])
        self.assertIn("+new", part["text"])

    def test_bash_result_extracts_stdout_and_stderr(self):
        obj = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]},
            "toolUseResult": {"stdout": "hello", "stderr": "warn"},
        }
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertIn("hello", part["text"])
        self.assertIn("warn", part["text"])

    def test_multiple_tool_results_in_one_line(self):
        obj = {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "b"}]},
        }
        parts = monitor.parse_msg_line(line(obj))["parts"]
        self.assertEqual([p["tool_use_id"] for p in parts], ["t1", "t2"])

    def test_meta_tool_note_is_marked(self):
        obj = {
            "type": "user",
            "isMeta": True,
            "sourceToolUseID": "t1",
            "message": {"content": [{"type": "text", "text": "skill body"}]},
        }
        msg = monitor.parse_msg_line(line(obj))
        self.assertEqual(msg["parts"][0]["type"], "tool_note")
        self.assertEqual(msg["parts"][0]["tool_use_id"], "t1")

    def test_boilerplate_meta_is_dropped(self):
        obj = {"type": "user", "isMeta": True,
               "message": {"content": [{"type": "text", "text": "boilerplate"}]}}
        self.assertIsNone(monitor.parse_msg_line(line(obj)))

    def test_tool_use_part_records_id(self):
        obj = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "a"}}]}}
        part = monitor.parse_msg_line(line(obj))["parts"][0]
        self.assertEqual(part["tool_use_id"], "t1")
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_transcript_parsing -v
```

- [ ] **Step 3: 实现**

新增辅助函数：

```python
def _patch_text(patch):
    lines = []
    for hunk in patch or []:
        if not isinstance(hunk, dict):
            continue
        lines.append(f"@@ -{hunk.get('oldStart', 0)},{hunk.get('oldLines', 0)} "
                     f"+{hunk.get('newStart', 0)},{hunk.get('newLines', 0)} @@")
        lines.extend(str(x) for x in (hunk.get("lines") or []))
    return "\n".join(lines)


def parse_tool_result(o, block):
    """把 tool_result + 行级 toolUseResult 解析为可展示结果。"""
    tur = o.get("toolUseResult")
    text = _result_text(block)
    kind = "error" if block.get("is_error") else "text"
    content = block.get("content")
    if isinstance(content, list) and any(
            isinstance(x, dict) and x.get("type") == "image" for x in content):
        kind, text = "image", "图片"
    if isinstance(tur, dict):
        patch = tur.get("structuredPatch")
        if isinstance(patch, list) and patch:
            kind, text = "diff", _patch_text(patch)
        elif "stdout" in tur or "stderr" in tur:
            chunks = []
            if tur.get("stdout"):
                chunks.append(str(tur["stdout"]))
            if tur.get("stderr"):
                chunks.append("[stderr]\n" + str(tur["stderr"]))
            if chunks:
                text = "\n".join(chunks)
        elif isinstance(tur.get("file"), dict):
            f = tur["file"]
            text = f"{f.get('filePath', '')} ({f.get('numLines', '?')} 行)"
        elif tur.get("type") == "image":
            kind, text = "image", str(tur.get("path") or tur.get("filePath") or "图片")
    return {"type": "tool_result", "tool_use_id": block.get("tool_use_id"),
            "is_error": bool(block.get("is_error")), "kind": kind,
            "text": clip(text, 8000)}
```

`parse_msg_line` 重写为：

```python
def parse_msg_line(ln, clip_len=8000):
    if b'"type"' not in ln:
        return None
    try:
        o = json.loads(ln)
    except json.JSONDecodeError:
        return None
    if o.get("isSidechain"):
        return None
    typ = o.get("type")
    if typ not in ("user", "assistant"):
        return None
    content = (o.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
    kind = classify_user_line(o, blocks) if typ == "user" else "assistant"
    if kind == "meta":
        return None
    role = {"user": "user", "injected": "system", "meta_tool": "system",
            "system": "system", "tool": "tool"}.get(kind, "assistant")
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and (b.get("text") or "").strip():
            if kind == "meta_tool":
                parts.append({"type": "tool_note",
                              "tool_use_id": o.get("sourceToolUseID"),
                              "text": clip(b["text"], 3000)})
            else:
                parts.append({"type": "text",
                              "text": clip(b["text"], 500 if role == "system" else clip_len)})
        elif bt == "tool_use":
            parts.append({"type": "tool", "tool_use_id": b.get("id"),
                          "name": b.get("name"),
                          "input": clip(b.get("input") or {}, 500)})
        elif bt == "tool_result":
            parts.append(parse_tool_result(o, b))
    if parts:
        return {"role": role, "ts": _parse_iso(o.get("timestamp")), "parts": parts}
    return None
```

说明：`meta_tool` 行的 `sourceToolUseID` 在行上，tool_result 块通常没有；因此走 text 分支生成 `tool_note`。

- [ ] **Step 4: 消息缓存合并**

在 `/sessions/<id>/messages` 分支中，初始化缓存时增加 `"tools": {}`：

```python
st = {"off": 0, "buf": b"", "msgs": [], "body": None, "tools": {}}
```

解析循环改为：

```python
for ln in lines:
    m = parse_msg_line(ln)
    if not m:
        continue
    keep = []
    for part in m["parts"]:
        if part.get("type") == "tool":
            if part.get("tool_use_id"):
                st["tools"][part["tool_use_id"]] = part
            keep.append(part)
        elif part.get("type") == "tool_result":
            target = st["tools"].get(part.get("tool_use_id"))
            if target is not None:
                target["result"] = part
            else:
                keep.append(part)
        elif part.get("type") == "tool_note":
            target = st["tools"].get(part.get("tool_use_id"))
            if target is not None:
                target.setdefault("notes", []).append(part["text"])
            else:
                keep.append({"type": "text", "text": part["text"]})
        else:
            keep.append(part)
    if keep:
        m["parts"] = keep
        st["msgs"].append(m)
```

- [ ] **Step 5: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_transcript_parsing -v
git add monitor.py tests/test_transcript_parsing.py
git commit -m "feat: 解析 toolUseResult 与 isMeta 注入"
```

---

### Task 5: 子代理 jsonl 发现

**Files:**
- Modify: `monitor.py`（`scan_once`、`unique_sessions`、`by_sid`、发送入口）
- Test: `tests/test_transcript_parsing.py`

- [ ] **Step 1: 写失败测试**

```python
class SubagentDiscoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(monitor, "CLAUDE_DIR", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        proj = self.root / "projects" / "p1"
        sub = proj / "parent-sid" / "subagents"
        sub.mkdir(parents=True)
        (proj / "parent-sid.jsonl").write_text(
            json.dumps({"type": "user", "cwd": str(self.root),
                        "message": {"content": "hello"}}) + "\n", encoding="utf-8")
        (sub / "agent-abc.jsonl").write_text(
            json.dumps({"type": "user", "cwd": str(self.root),
                        "attributionAgent": "Explore",
                        "message": {"content": "sub task"}}) + "\n", encoding="utf-8")
        self.mon = monitor.Monitor(600)

    def test_subagent_is_discovered_and_hidden_by_default(self):
        self.mon.scan_once()
        main = self.mon.unique_sessions()
        self.assertEqual([s.id for s in main], ["parent-sid"])
        all_sessions = self.mon.unique_sessions(include_subagents=True)
        ids = {s.id for s in all_sessions}
        self.assertIn("parent-sid::agent-agent-abc", ids)
        sub = self.mon.by_sid("parent-sid::agent-agent-abc")
        self.assertTrue(sub.is_subagent)
        self.assertEqual(sub.parent_id, "parent-sid")
        self.assertEqual(sub.agent_name, "Explore")

    def test_send_to_subagent_is_rejected(self):
        self.mon.scan_once()
        code, _ = self.mon.send_message("parent-sid::agent-agent-abc", "hi")
        self.assertEqual(code, 400)
```

- [ ] **Step 2: 实现扫描**

`scan_once` 改为：

```python
paths = list(root.glob("*/*.jsonl"))
paths.extend(root.glob("*/*/subagents/agent-*.jsonl"))
for path in paths:
    is_sub = len(path.parts) >= 2 and path.parent.name == "subagents"
    if is_sub:
        parent_id = path.parent.parent.name
        sid = f"{parent_id}::agent-{path.stem}"
    else:
        parent_id, sid = None, path.stem
    ...
    if sess is None:
        sess = self.sessions[key] = Session(sid, path)
        sess.is_subagent = is_sub
        sess.parent_id = parent_id
```

`handle_line` 增加：

```python
if obj.get("attributionAgent") and not sess.agent_name:
    sess.agent_name = obj["attributionAgent"]
if sess.is_subagent and not sess.cwd:
    sess.cwd = obj.get("cwd") or sess.cwd
```

`unique_sessions(self, include_subagents=False)`：

```python
return [s for s in best.values() if include_subagents or not s.is_subagent]
```

`send_message` / `host_session`：找到 `sess` 后若 `sess.is_subagent` 返回 400「子代理会话只读」。

- [ ] **Step 3: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_transcript_parsing -v
git add monitor.py tests/test_transcript_parsing.py
git commit -m "feat: 发现只读子代理会话"
```

---

### Task 6: 审批交互模型与 `can_use_tool` 协议

**Files:**
- Modify: `monitor.py`（新增 `Interaction`、`strip_ansi`、`permission_preview`、`Hosted`）
- Modify: `tests/fake_claude.py`
- Test: `tests/test_interactions.py`（新建）

- [ ] **Step 1: 扩展 fake_claude.py**

在 `for line in sys.stdin` 循环内新增处理（保留现有逻辑）：

```python
    elif obj["type"] == "user":
        emit({"type": "system", "subtype": "init", "session_id": "fake-session"})
        text = obj["message"]["content"]
        uuid = obj.get("uuid")
        emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "started"})
        if text.startswith("permission"):
            emit({"type": "control_request", "request_id": "req-1", "request": {
                "subtype": "can_use_tool", "tool_name": "Edit", "tool_use_id": "tool-1",
                "input": {"file_path": "/tmp/a", "old_string": "old", "new_string": "new"},
                "permission_suggestions": [{"type": "addRules", "behavior": "allow",
                                            "destination": "session",
                                            "rules": [{"toolName": "Edit"}]}],
                "decision_reason": "writes a file"}})
            # 等待 control_response（由外层循环继续读取 stdin）
            continue
        if text == "slow":
            time.sleep(0.15)
        emit({"type": "result", "session_id": "fake-session", "result": str(model) + ":" + text})
        emit({"type": "command_lifecycle", "command_uuid": uuid, "state": "completed"})
```

问题：`continue` 会回到 for 循环等待下一行；control_response 行会被 `obj["type"] == "control_response"` 分支忽略。需要在该分支里识别并 emit result：

```python
    elif obj["type"] == "control_response" and obj.get("response", {}).get("request_id") == "req-1":
        response = obj["response"].get("response") or {}
        emit({"type": "result", "session_id": "fake-session",
              "result": "decision:" + str(response.get("behavior"))})
        emit({"type": "command_lifecycle", "command_uuid": "permission-uuid", "state": "completed"})
```

为了让生命周期匹配，permission 测试不依赖 job 的 command_uuid 完成（fallback 3 秒太慢）。改用：控制响应后 emit `command_lifecycle` 的 command_uuid 从本次 user 的 uuid 保存到模块变量。实现时用 `pending_uuid` 变量记录。

同时保留旧行为（不依赖 lifecycle 的测试仍可通过 result 兜底，但 3 秒太慢）。因此所有 user 分支都 emit lifecycle；现有测试会在 lifecycle 到达后立即完成。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_interactions.py`，参照 `test_model_execution.py` 的 fake 进程启动方式：

```python
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(monitor, "CLAUDE_DIR", self.root / "config")
        self.patch.start()
        self.mon = monitor.Monitor(600, permission_timeout=0.3)
        self.sess = monitor.Session("fake-session", self.root / "fake.jsonl")
        self.sess.cwd = str(self.root)
        self.mon.sessions[str(self.sess.path)] = self.sess
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.stop(kill=True)
            if host.proc:
                try:
                    host.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.patch.stop()
        self.tmp.cleanup()

    def start_host(self, text="first", approval="ask"):
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **kwargs):
            return original([sys.executable, fake] + cmd[1:], **kwargs)
        with patch.object(monitor.subprocess, "Popen", side_effect=popen):
            code, resp = self.mon.host_session(str(self.root), None, text,
                                               model="opus", approval=approval)
        self.assertEqual(code, 202)
        host = self.mon.hosted[resp["hosted_id"]]
        self.hosts.append(host)
        return host, resp["job_id"]

    def wait_for(self, cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_permission_allow_once(self):
        host, job = self.start_host("permission please")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        pending = self.mon.pending_interactions("fake-session")[0]
        self.assertEqual(pending["tool_name"], "Edit")
        self.assertIn("-old", pending["preview"]["text"])
        code, _ = self.mon.respond_interaction(pending["id"], {"behavior": "allow"})
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertEqual(self.mon.jobs[job]["result"], "decision:allow")

    def test_permission_timeout_denies(self):
        host, job = self.start_host("permission please")
        self.assertTrue(self.wait_for(
            lambda: self.mon.jobs[job]["status"] in ("done", "failed", "cancelled")))
        self.assertEqual(self.mon.jobs[job]["result"], "decision:deny")

    def test_full_mode_auto_allows(self):
        host, job = self.start_host("permission please", approval="full")
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[job]["status"] == "done"))
        self.assertEqual(self.mon.jobs[job]["result"], "decision:allow")

    def test_allow_always_requires_suggestion(self):
        host, job = self.start_host("permission please")
        self.assertTrue(self.wait_for(lambda: self.mon.pending_interactions("fake-session")))
        pending = self.mon.pending_interactions("fake-session")[0]
        self.assertTrue(pending["allow_always"])
```

- [ ] **Step 3: 实现 Interaction**

在 `monitor.py` 顶部增加 `import difflib`、`import re`、`import uuid`。

`Session` 之前或 `Hosted` 之前加入：

```python
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text):
    return _ANSI.sub("", text or "")


def permission_preview(tool_name, tool_input):
    ti = tool_input or {}
    if tool_name == "Write":
        path = ti.get("file_path") or ti.get("path") or "new file"
        diff = "\n".join(difflib.unified_diff([], (ti.get("content") or "").splitlines(),
                                              fromfile="/dev/null", tofile=path, lineterm=""))
        return {"kind": "diff", "text": clip(diff, 8000)}
    if "old_string" in ti and "new_string" in ti:
        path = ti.get("file_path") or ti.get("path") or ""
        diff = "\n".join(difflib.unified_diff(ti.get("old_string", "").splitlines(),
                                              ti.get("new_string", "").splitlines(),
                                              fromfile=path, tofile=path, lineterm=""))
        return {"kind": "diff", "text": clip(diff, 8000)}
    if "command" in ti:
        return {"kind": "text", "text": clip(ti.get("command", ""), 4000)}
    return {"kind": "text", "text": clip(json.dumps(ti, ensure_ascii=False), 4000)}


class Interaction:
    def __init__(self, request_id, hosted, request):
        self.id = request_id
        self.hosted_id = hosted.hid
        self.session_id = hosted.session_id
        self.request = request
        self.tool_name = request.get("tool_name") or ""
        self.tool_use_id = request.get("tool_use_id")
        self.tool_input = request.get("input") or {}
        self.reason = strip_ansi(request.get("decision_reason") or "")
        self.suggestions = [s for s in (request.get("permission_suggestions") or [])
                            if isinstance(s, dict) and s.get("behavior") == "allow"]
        self.allow_always = bool(self.suggestions) and not request.get("suppress_always_allow_rule")
        self.preview = permission_preview(self.tool_name, self.tool_input)
        self.created_at = now_ts()
        self.event = threading.Event()
        self.behavior = None
        self.scope = "once"
        self.message = None
        self.updated_input = self.tool_input
        self.state = "pending"
        self.resolved_at = None

    def resolve(self, behavior, scope="once", reason=None, state=None):
        if self.state != "pending":
            return False
        self.behavior = behavior
        self.scope = scope
        self.message = reason
        self.state = state or ("allowed" if behavior == "allow" else "denied")
        self.resolved_at = now_ts()
        self.event.set()
        return True

    def public(self):
        return {"id": self.id, "hosted_id": self.hosted_id, "session_id": self.session_id,
                "tool_name": self.tool_name, "tool_use_id": self.tool_use_id,
                "tool_input": self.tool_input, "preview": self.preview,
                "reason": self.reason, "allow_always": self.allow_always,
                "created_at": self.created_at, "state": self.state,
                "behavior": self.behavior, "scope": self.scope,
                "resolved_at": self.resolved_at}
```

`Monitor` 增加：

```python
def pending_interactions(self, sid=None):
    with self.lock:
        items = [i.public() for i in self.interactions.values()
                 if i.state == "pending" and (sid is None or i.session_id == sid)]
    return sorted(items, key=lambda item: item["created_at"])

def respond_interaction(self, iid, body):
    if not isinstance(body, dict):
        return 400, {"error": "invalid JSON body"}
    behavior = body.get("behavior")
    if behavior not in ("allow", "deny"):
        return 400, {"error": "behavior 必须是 allow 或 deny"}
    scope = body.get("scope", "once")
    if scope not in ("once", "always"):
        return 400, {"error": "scope 必须是 once 或 always"}
    if scope == "always" and behavior != "allow":
        return 400, {"error": "scope=always 只能用于 allow"}
    with self.lock:
        inter = self.interactions.get(iid)
        if inter is None:
            return 404, {"error": "interaction not found"}
        if inter.state != "pending":
            return 409, {"error": "interaction already resolved"}
        if scope == "always" and not inter.allow_always:
            return 400, {"error": "该请求不允许始终允许"}
    reason = body.get("reason")
    reason = clip(reason, 500) if isinstance(reason, str) else None
    inter.resolve(behavior, scope, reason)
    return 200, inter.public()
```

`Hosted.__init__` 增加：

```python
self.approval = approval or (mon.approvals.get(session_id) if session_id else "ask")
self.interrupting = False
```

`Hosted.__init__` 签名改为 `def __init__(self, mon, cwd, resume_id=None, selected_model=None, approval=None)`。`Monitor.host_session` / `_host_session_locked` 增加 `approval=None` 参数，并传给 `Hosted(...)`：

```python
h = Hosted(self, cwd, resume_id, selected, approval=approval or
           (self.approvals.get(resume_id) if resume_id else "ask"))
```

`do_POST /hosted` 读取 `body.get("approval")` 并传入。这样审批模式在进程启动前生效，避免首个工具调用与模式设置竞争。

- [ ] **Step 4: 实现权限协议**

在 `Hosted` 中增加：

```python
def _sess(self):
    with self.mon.lock:
        return self.mon.by_sid(self.session_id)

def _emit_session_event(self, kind, data):
    sess = self._sess()
    if sess:
        self.mon.emit(sess, kind, data)

def _on_can_use_tool(self, obj):
    request_id = obj.get("request_id")
    if not request_id:
        return
    inter = Interaction(request_id, self, obj.get("request") or {})
    with self.mon.lock:
        self.mon.interactions[request_id] = inter
    self._emit_session_event("interaction_pending", inter.public())
    if self.approval == "full":
        inter.resolve("allow", "once", state="allowed")
        self._write_interaction(inter)
    else:
        threading.Thread(target=self._await_interaction, args=(inter,), daemon=True).start()

def _await_interaction(self, inter):
    timeout = self.mon.permission_timeout
    inter.event.wait(timeout if timeout and timeout > 0 else None)
    if inter.state == "pending":
        inter.resolve("deny", reason="审批超时，已自动拒绝", state="timed_out")
    self._write_interaction(inter)

def _write_interaction(self, inter):
    if inter.behavior == "allow":
        response = {"behavior": "allow", "updatedInput": inter.updated_input}
        if inter.scope == "always":
            response["updatedPermissions"] = inter.suggestions
    else:
        response = {"behavior": "deny", "message": inter.message or "用户拒绝"}
    payload = {"type": "control_response", "response": {
        "subtype": "success", "request_id": inter.id, "response": response}}
    try:
        if self.status != "exited":
            self._write(payload)
    except (OSError, ValueError):
        if inter.state == "pending":
            inter.state = "cancelled"
    with self.mon.lock:
        self.mon.interactions.pop(inter.id, None)
    self._emit_session_event("interaction_resolved", inter.public())
```

`_read_out` 分派增加：

```python
elif typ == "control_request" and (obj.get("request") or {}).get("subtype") == "can_use_tool":
    self._on_can_use_tool(obj)
elif typ == "control_cancel_request":
    with self.mon.lock:
        inter = self.mon.interactions.get(obj.get("request_id"))
    if inter:
        inter.resolve("deny", reason="请求已被 Claude 取消", state="cancelled")
elif typ == "command_lifecycle":
    self._on_lifecycle(obj)
```

- [ ] **Step 5: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_interactions -v
git add monitor.py tests/fake_claude.py tests/test_interactions.py
git commit -m "feat: 托管会话权限审批协议"
```

---

### Task 7: `command_lifecycle`、中断与队列上限

**Files:**
- Modify: `monitor.py`（`Hosted`）
- Modify: `tests/fake_claude.py`
- Test: `tests/test_interactions.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_queue_limit(self):
        host, first = self.start_host("slow")
        host.max_queued = 2
        host.send("a")
        host.send("b")
        code, resp = host.send("c")
        self.assertEqual(code, 429)
        self.assertIn("queue", resp["error"].lower())

    def test_interrupt_clears_queue_and_cancels_current(self):
        host, first = self.start_host("slow")
        host.send("queued")
        code, _ = self.mon.interrupt_session("fake-session")
        self.assertEqual(code, 200)
        self.assertTrue(self.wait_for(lambda: self.mon.jobs[first]["status"] == "cancelled"))
```

fake 的 `slow` 需要阻塞到收到 interrupt。修改 fake：当 text == "slow" 时，等待 stdin 上的 interrupt 控制请求，收到后 emit lifecycle cancelled。

- [ ] **Step 2: 实现 Hosted 队列/生命周期**

`Hosted.__init__` 增加：

```python
self.max_queued = 32
self.current = None
self.pending = deque()
self.interrupting = False
self.lifecycle_seen = False
```

`send()` 入队前检查：

```python
if len(self.pending) >= self.max_queued:
    return 429, {"error": "hosted send queue is full"}
```

`_send_loop` 写用户消息带 uuid：

```python
job["command_uuid"] = job.get("command_uuid") or str(uuid.uuid4())
self._write({"type": "user", "uuid": job["command_uuid"],
             "message": {"role": "user", "content": text}})
```

`_new_job` 后设置 uuid（在 `Hosted.send` 中）：

```python
job["command_uuid"] = str(uuid.uuid4())
```

`_on_lifecycle`：

```python
def _on_lifecycle(self, obj):
    state = obj.get("state")
    cu = obj.get("command_uuid")
    self.lifecycle_seen = True
    with self.ready:
        job = self.current if self.current and self.current.get("command_uuid") == cu else None
    if job is None:
        self._emit_session_event("hosted_foreign_turn", {"hosted": self.hid, "state": state})
        return
    job["_terminal_state"] = state
    if state != "started":
        if state == "completed":
            threading.Timer(3.0, lambda: self._finish_job(job, "completed")).start()
        else:
            self._finish_job(job, state)
```

`_finish_job(job, terminal_state)`：

```python
def _finish_job(self, job, terminal_state):
    with self.ready:
        if job.get("finished_at"):
            return
        if terminal_state == "completed":
            status = "failed" if job.get("result_is_error") else "done"
            error = job.get("error")
        elif terminal_state == "cancelled":
            status, error = "cancelled", job.get("error") or "已中断"
        else:
            status, error = "failed", job.get("error") or f"command {terminal_state}"
        job.update(status=status, error=error, finished_at=now_ts())
        if self.current is job:
            self.current = None
        self.processing = False
        self.interrupting = False
        self.ready.notify_all()
        self.turns += 1
    self._emit_session_event("hosted_turn_done", {
        "hosted": self.hid, "job": job.get("id"), "status": job.get("status"),
        "result": clip(job.get("result") or "", 300),
        "is_error": job.get("result_is_error", False)})
```

`_on_result` 改为：

```python
def _on_result(self, obj):
    with self.ready:
        job = self.current
        if job is None:
            return
        job["result"] = obj.get("result")
        job["result_session_id"] = obj.get("session_id")
        job["cost_usd"] = obj.get("total_cost_usd")
        job["result_is_error"] = bool(obj.get("is_error"))
        if obj.get("is_error"):
            job["error"] = clip(obj.get("result") or obj.get("subtype") or "error", 500)
        terminal = job.get("_terminal_state")
        lifecycle_seen = self.lifecycle_seen
    if terminal:
        self._finish_job(job, terminal)
    elif not lifecycle_seen:
        threading.Timer(3.0, lambda: self._finish_job(job, "completed")).start()
```

`interrupt_session(sid)`：

```python
def interrupt_session(self, sid):
    with self.lock:
        h = self.hosted_by_sid.get(sid)
    if not h or not h.alive():
        return 409, {"error": "session is not hosted"}
    return h.interrupt()

def interrupt(self):
    if not self.alive():
        return 409, {"error": "hosted process is not running"}
    with self.ready:
        self.interrupting = True
        for _, job, _ in self.pending:
            job.update(status="cancelled", error="已中断", finished_at=now_ts())
        self.pending.clear()
        job = self.current
        self.ready.notify_all()
    try:
        self._write({"type": "control_request",
                     "request_id": f"monitor-int-{self.hid}-{int(time.time() * 1000)}",
                     "request": {"subtype": "interrupt"}})
    except (OSError, ValueError) as exc:
        if job:
            self._finish_job(job, "failed")
        return 500, {"error": f"中断写入失败: {exc}"}
    if job:
        threading.Timer(30.0, lambda: self._finish_job(job, "cancelled")).start()
    else:
        with self.ready:
            self.interrupting = False
    return 200, {"interrupted": True, "queued_cancelled": True}
```

进程退出时清理交互：在 `_read_out` 末尾的状态更新处，先设置 `self.status="exited"`，再调用：

```python
with self.mon.lock:
    items = [i for i in self.mon.interactions.values() if i.hosted_id == self.hid]
for inter in items:
    inter.resolve("deny", reason="托管进程已退出", state="cancelled")
```

- [ ] **Step 3: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_interactions -v
git add monitor.py tests/fake_claude.py tests/test_interactions.py
git commit -m "feat: 托管回合生命周期与中断"
```

---

### Task 8: HTTP 端点与详情字段

**Files:**
- Modify: `monitor.py`（`make_handler`、`Session.detail` 相关）
- Test: `tests/test_management_api.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_interactions_endpoint(self):
        inter = monitor.Interaction("r1", type("H", (), {"hid": "h1", "session_id": "sid-1"})(),
                                    {"tool_name": "Bash", "tool_use_id": "t1",
                                     "input": {"command": "ls"}})
        self.mon.interactions["r1"] = inter
        result = json.load(self.request("/interactions?session=sid-1"))
        self.assertEqual(result["interactions"][0]["tool_name"], "Bash")

    def test_approval_endpoint_persists(self):
        response = self.request("/sessions/sid-1/approval", {"approval": "full"},
                                {"Content-Type": "application/json"})
        self.assertEqual(json.load(response)["approval"], "full")
        self.assertEqual(self.mon.approvals.get("sid-1"), "full")
        detail = json.load(self.request("/sessions/sid-1"))
        self.assertEqual(detail["approval"], "full")

    def test_interrupt_non_hosted_returns_409(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/sessions/sid-1/interrupt", {}, {"Content-Type": "application/json"})
        self.assertEqual(error.exception.code, 409)
```

- [ ] **Step 2: 实现路由**

`do_GET` 增加：

```python
if u.path == "/interactions":
    sid = parse_qs(u.query).get("session", [None])[0]
    return self._json({"interactions": mon.pending_interactions(sid)})
```

`/sessions` 处理：

```python
qs = parse_qs(u.query)
include_subagents = qs.get("include_subagents", ["0"])[0] in ("1", "true")
...
sessions = mon.unique_sessions(include_subagents=include_subagents)
items = sorted((... for s in sessions),
               key=lambda x: x["last_input_at"] or x["last_event_at"], reverse=True)
with mon.lock:
    counts = Counter(i.session_id for i in mon.interactions.values() if i.state == "pending")
for it in items:
    it["pending_interactions"] = counts.get(it["id"], 0)
```

`/sessions/<id>` 详情增加：

```python
d["interactions"] = mon.pending_interactions(sid)
d["approval"] = mon.approvals.get(sid)
d["subagent_session_ids"] = sorted({s.id for s in mon.sessions.values()
                                    if s.parent_id == sid})
h = mon.hosted_by_sid.get(sid)
d["hosted_busy"] = bool(h and h.alive() and (h.current or h.pending or h.processing))
d["hosted_queued"] = len(h.pending) if h and h.alive() else 0
```

`do_POST` 增加：

```python
if u.path.startswith("/interactions/") and u.path.endswith("/respond"):
    if not self._safe_json_write():
        return
    iid = u.path.split("/")[2]
    code, resp = mon.respond_interaction(iid, self._read_body())
    return self._json(resp, code)

parts = u.path.strip("/").split("/")
if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "interrupt":
    if not self._safe_json_write():
        return
    code, resp = mon.interrupt_session(parts[1])
    return self._json(resp, code)

if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "approval":
    if not self._safe_json_write():
        return
    body = self._read_body()
    mode = body.get("approval") if isinstance(body, dict) else None
    if mode not in APPROVAL_MODES:
        return self._json({"error": "approval 必须是 ask / auto / full"}, 400)
    try:
        mon.approvals.set(parts[1], mode)
    except ValueError as e:
        return self._json({"error": str(e)}, 500)
    with mon.lock:
        h = mon.hosted_by_sid.get(parts[1])
    if h and h.alive():
        try:
            h.set_approval(mode)
        except (RuntimeError, TimeoutError) as e:
            return self._json({"error": f"实时切换失败: {e}"}, 409)
    return self._json({"approval": mode})
```

`Hosted.set_approval`：

```python
def set_approval(self, mode):
    if mode not in APPROVAL_MODES:
        raise ValueError("invalid approval mode")
    target = "auto" if mode == "auto" else "default"
    if self.approval == "auto" or mode == "auto":
        self._control("set_permission_mode", mode=target)
    self.approval = mode
```

托管启动接入审批模式：`host_session(..., approval=None)`；若 `approval` 为 None 则读取持久化值：

```python
h = Hosted(self, cwd, resume_id, selected, approval=approval or
           self.approvals.get(resume_id or ""))
```

`Hosted.__init__(..., approval="ask")`。

`/hosted` info 增加 `approval`、`current_job`、`queued`、`interrupting`。

- [ ] **Step 3: 运行测试并提交**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api -v
git add monitor.py tests/test_management_api.py
git commit -m "feat: 审批与中断 HTTP 接口"
```

---

### Task 9: 前端审批卡片、中断与富工具结果

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_frontend_controls.js`（现有 JS 测试可补充静态断言）

- [ ] **Step 1: 前端渲染**

在 CSS 中增加：

```css
.tool-details{margin:3px 0;font-size:12px}
.tool-details>summary{cursor:pointer;color:var(--mut);font-family:ui-monospace,monospace;
  background:var(--card2);border:1px solid var(--bd);border-radius:6px;padding:2px 8px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tool-out{margin:4px 0;max-height:320px;overflow:auto;background:var(--card2);
  border:1px solid var(--bd);border-radius:6px;padding:6px 8px;white-space:pre-wrap}
.diff-add{color:#16a34a}.diff-del{color:#dc2626}.diff-hunk{color:var(--acc)}
.inter-card{background:var(--card);border:1px solid #d97706;border-radius:8px;padding:10px 12px;margin:8px 0}
.inter-card pre{white-space:pre-wrap;max-height:240px;overflow:auto}
```

在 `convHtml` 中替换 tool/error part 渲染：

```js
function diffHtml(text){
  return String(text || "").split("\n").map(line => {
    const cls = line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-del"
              : line.startsWith("@@") ? "diff-hunk" : "";
    return `<span class="${cls}">${esc(line)}</span>`;
  }).join("\n");
}
function toolResultHtml(p){
  const r = p.result;
  if(!r) return "";
  if(r.kind === "diff") return `<pre class="tool-out">${diffHtml(r.text)}</pre>`;
  if(r.kind === "image") return `<div class="mut">[图片] ${esc(r.text || "")}</div>`;
  if(r.is_error || r.kind === "error") return `<div class="terror">✗ ${esc(r.text || "")}</div>`;
  return `<pre class="tool-out">${esc(r.text || "")}</pre>`;
}
function toolPartHtml(p){
  if(p.type === "tool_result"){
    return `<div class="tool">🔧 工具结果${p.is_error ? " (错误)" : ""}</div>` + toolResultHtml(p);
  }
  if(p.type === "tool_note") return `<div class="sysbody">${esc(p.text)}</div>`;
  const notes = (p.notes || []).map(esc).join("\n");
  return `<details class="tool-details"><summary>🔧 ${esc(p.name)}: ${esc(p.input)}</summary>` +
    (notes ? `<div class="mut">${esc(notes)}</div>` : "") + toolResultHtml(p) + `</details>`;
}
```

在 `convHtml` 的 part 映射中用 `toolPartHtml(p)` 替换 tool 和 error 分支。

- [ ] **Step 2: 审批卡片**

增加：

```js
let interCache = {id: null, sig: ""};
function interactionsHtml(items){
  if(!items || !items.length) return "";
  return items.map(it => {
    const preview = it.preview || {};
    const body = preview.kind === "diff" ? diffHtml(preview.text)
               : `<pre>${esc(preview.text || "")}</pre>`;
    return `<div class="inter-card" data-iid="${esc(it.id)}">
      <div><b>需要审批</b> · <code>${esc(it.tool_name)}</code></div>
      ${it.reason ? `<div class="mut">${esc(it.reason)}</div>` : ""}
      ${body}
      <div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap">
        <button class="back" data-act="allow" data-scope="once">允许一次</button>
        ${it.allow_always ? '<button class="back" data-act="allow" data-scope="always">始终允许</button>' : ""}
        <button class="back" data-act="deny">拒绝</button>
      </div></div>`;
  }).join("");
}
function renderInteractions(items){
  const el = document.getElementById("interactions");
  if(!el) return;
  const sig = JSON.stringify((items || []).map(it => [it.id, it.state]));
  if(interCache.sig === sig) return;
  interCache.sig = sig;
  el.innerHTML = interactionsHtml(items);
}
async function respondInteraction(card, act, scope){
  const iid = card.dataset.iid;
  const buttons = card.querySelectorAll("button");
  buttons.forEach(b => b.disabled = true);
  try{
    const r = await fetchT("/interactions/" + encodeURIComponent(iid) + "/respond", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({behavior: act, scope}),
    });
    const j = await r.json();
    if(!r.ok) throw Error(j.error || ("HTTP " + r.status));
    interCache.sig = "";
    load();
  }catch(e){
    card.insertAdjacentHTML("beforeend", `<div class="terror">${esc(e.message)}</div>`);
    buttons.forEach(b => b.disabled = false);
  }
}
```

在对话面板 HTML 中加入 `<div id="interactions"></div>`（发送区上方）。在 `main` 的点击委托中增加：

```js
const act = e.target.closest("#interactions button[data-act]");
if(act){
  const card = act.closest(".inter-card");
  respondInteraction(card, act.dataset.act, act.dataset.scope || "once");
  return;
}
```

首次渲染对话面板时重置 `interCache.sig = ""`。

- [ ] **Step 3: 中断与审批模式控件**

在对话面板的发送行加入：

```html
<button class="back" id="intbtn">中断</button>
<label class="ctl" id="approvalwrap">审批
  <select id="approvalsel">
    <option value="ask">询问</option><option value="auto">自动</option><option value="full">全部允许</option>
  </select>
</label>
```

`bindSendBox` 中：

```js
const intBtn = document.getElementById("intbtn");
if(intBtn) intBtn.onclick = async () => {
  intBtn.disabled = true;
  try{
    const r = await fetchT("/sessions/" + encodeURIComponent(id) + "/interrupt", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    const j = await r.json();
    if(!r.ok) document.getElementById("sendst").textContent = "中断失败: " + (j.error || r.status);
  }catch(e){ document.getElementById("sendst").textContent = "中断错误: " + e; }
  finally{ intBtn.disabled = false; load(); }
};
const approvalSel = document.getElementById("approvalsel");
if(approvalSel) approvalSel.onchange = async () => {
  const r = await fetchT("/sessions/" + encodeURIComponent(id) + "/approval", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({approval: approvalSel.value})});
  const j = await r.json();
  document.getElementById("sendst").textContent = r.ok ? "审批模式已更新" : ("失败: " + (j.error || r.status));
};
```

首次渲染和 `keep` 分支都设置：

```js
const approvalSel = document.getElementById("approvalsel");
if(approvalSel) approvalSel.value = d.approval || "ask";
const intBtn = document.getElementById("intbtn");
if(intBtn){ intBtn.disabled = !(d.hosted && d.hosted_busy); intBtn.title = d.hosted_queued ? `队列 ${d.hosted_queued}` : ""; }
```

在 `renderConversation` 的 `keep` 分支调用 `renderInteractions(d.interactions)`；首次渲染也在 `#interactions` 容器内调用。

- [ ] **Step 4: 运行前端测试与提交**

```bash
node tests/test_frontend_controls.js
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
git add templates/index.html
git commit -m "feat: 前端审批卡片与中断控件"
```

---

### Task 10: 全量验证

- [ ] **Step 1: 运行全部 Python 测试**

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: 全部 PASS（47 个旧测试 + 新增测试）。

- [ ] **Step 2: 运行前端静态测试**

```bash
node tests/test_frontend_controls.js
```

- [ ] **Step 3: 手工冒烟（可选，需要可用 claude 凭据）**

```bash
uv run monitor.py --port 8799 --permission-timeout 60
```

打开 `http://127.0.0.1:8799`，对某会话点击「托管」，发送一个会触发工具调用的消息，确认：
- 对话视图出现审批卡片；
- 允许后回合继续；
- 「中断」能结束当前回合且进程保留；
- 完成后状态变为空闲。

- [ ] **Step 4: 更新 CLAUDE.md**

在「关键设计决策」中补充：

- 托管进程通过 `--permission-prompt-tool stdio` 接收 `can_use_tool`，审批模式为 ask/auto/full，超时默认 300 秒自动拒绝；
- 回合结束以 `system/turn_duration` 为准，`stop_reason=end_turn` 仅作无该标记时的回退；
- `last_input_ts` 与 `last_event_ts` 双时钟；
- 子代理文件 `subagents/agent-*.jsonl` 作为只读会话。

提交：

```bash
git add CLAUDE.md
git commit -m "docs: 更新托管审批与回合判定说明"
```