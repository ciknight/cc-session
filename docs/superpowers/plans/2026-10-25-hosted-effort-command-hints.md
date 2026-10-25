# 托管 effort 在线切换与斜杠指令提示 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 托管会话支持在线切换思考档位（effort，即时 `apply_flag_settings` + 持久化 + spawn `--effort`），发送框支持 `/` 指令提示面板（来自 `system/init` 的 `slash_commands`，发送不校验）。

**Architecture:** 后端在 `session_preferences.py` 加 `EffortPreferences`（照 `ApprovalPreferences` 模式）与 `effort_args`（照 `model_args` 模式）；`monitor.py` 的 `Hosted` 记录档位、spawn 带参、`set_effort` 走既有 `_control` 通道，`_on_init` 顺带捕获指令目录；HTTP 加 `POST /sessions/<sid>/effort`，`/hosted` body 加 `effort`，detail 加 `effort`/`commands`。前端发送区加"思考"下拉（交互同审批下拉），msgbox 下加指令提示面板（纯函数过滤 + ↑↓/Tab/Esc 键盘、Enter 直发不拦截）。

**Tech Stack:** Python 标准库（unittest）、单文件 SPA `templates/index.html`（无构建）、`node --test` 前端测试。

设计 spec：`docs/superpowers/specs/2026-10-25-hosted-effort-command-hints-design.md`

---

### Task 1: EffortPreferences 与 effort_args（session_preferences.py）

**Files:**
- Modify: `session_preferences.py`
- Test: `tests/test_session_preferences.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_session_preferences.py` 末尾（`ApprovalPreferenceTests` 之后）追加：

```python
class EffortPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "efforts.json"
        self.addCleanup(self.tmp.cleanup)

    def test_validate_effort(self):
        self.assertEqual(prefs.validate_effort("low"), "low")
        self.assertEqual(prefs.validate_effort("high"), "high")
        self.assertIsNone(prefs.validate_effort("default"))
        for level in ("", "default ", "high ", "ultra", None, 1, [], {}):
            with self.subTest(level=level), self.assertRaises(ValueError):
                prefs.validate_effort(level)

    def test_arguments_override_only_effort(self):
        args = ["--effort", "low", "--verbose", "--effort=high"]
        self.assertEqual(prefs.effort_args(args, None), args)
        self.assertEqual(prefs.effort_args(args, "medium"), ["--verbose", "--effort=medium"])
        self.assertEqual(args[1], "low")

    def test_persist_and_clear_by_session(self):
        store = prefs.EffortPreferences(self.path)
        self.assertIsNone(store.get("a"))
        store.set("a", "high")
        store.set("b", "low")
        self.assertEqual(prefs.EffortPreferences(self.path).get("a"), "high")
        store.set("a", None)
        self.assertIsNone(store.get("a"))
        self.assertEqual(store.get("b"), "low")
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["version"], 1)

    def test_invalid_level_rejected_and_corrupt_file(self):
        store = prefs.EffortPreferences(self.path)
        with self.assertRaises(ValueError):
            store.set("a", "ultra")
        self.assertIsNone(store.get("a"))
        self.path.write_text("broken", encoding="utf-8")
        broken = prefs.EffortPreferences(self.path)
        self.assertTrue(broken.error)
        with self.assertRaises(ValueError):
            broken.set("a", "low")
        self.assertEqual(self.path.read_text("utf-8"), "broken")

    def test_bind_does_not_replace_later_choice(self):
        store = prefs.EffortPreferences(self.path)
        store.set("a", "low")
        store.bind("a", "high")
        self.assertEqual(store.get("a"), "low")
        store.bind("b", "medium")
        self.assertEqual(store.get("b"), "medium")
```

- [ ] **Step 2: 运行确认失败**

```
PYTHONPATH=. uv run python -m unittest tests.test_session_preferences -v
```
预期：FAIL/ERROR，`module 'session_preferences' has no attribute 'validate_effort'`（或 EffortPreferences）。

- [ ] **Step 3: 实现**

`session_preferences.py` 中，`APPROVAL_MODES` 定义之后（约 line 12 附近）加：

```python
EFFORT_LEVELS = ("low", "medium", "high")
```

`model_args` 之后加两个函数：

```python
def validate_effort(level):
    """effort 接口值：low / medium / high；default 规范化为 None（模型默认）。"""
    if level == "default":
        return None
    if level not in EFFORT_LEVELS:
        raise ValueError("effort 必须是 low / medium / high / default")
    return level


def effort_args(args, effort):
    """有档位时剥掉用户自带的 --effort 再追加，模式与 model_args 一致。"""
    if effort is None:
        return list(args)
    result = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--effort":
            i += 2
            continue
        if not arg.startswith("--effort="):
            result.append(arg)
        i += 1
    return result + ["--effort=" + validate_effort(effort)]
```

文件末尾（`ApprovalPreferences` 之后）加类，结构照 `ApprovalPreferences`，落盘键名 `efforts`：

```python
class EffortPreferences:
    """看板独立的托管思考档位；spawn 带 --effort，使重托管后保持。"""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.efforts = {}
        self.touched = set()
        self.error = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != 1
                    or not isinstance(data.get("efforts"), dict)):
                raise ValueError("格式或版本不支持")
            for sid, level in data["efforts"].items():
                if not isinstance(sid, str) or level not in EFFORT_LEVELS:
                    raise ValueError("思考档位记录无效")
            self.efforts = data["efforts"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.error = "思考档位文件无法读取或格式损坏，请先修复或移走该文件"

    def get(self, sid):
        with self.lock:
            return self.efforts.get(sid)

    def set(self, sid, level):
        if level is not None and level not in EFFORT_LEVELS:
            raise ValueError("思考档位必须是 low / medium / high，或 null")
        with self.lock:
            if self.error:
                raise ValueError(self.error)
            updated = dict(self.efforts)
            if level is None:
                updated.pop(sid, None)
            else:
                updated[sid] = level
            temp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temp = tempfile.mkstemp(prefix="efforts-", suffix=".tmp", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "efforts": updated}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
            except OSError as exc:
                raise ValueError("思考档位保存失败，请检查目录权限或磁盘空间") from exc
            finally:
                if temp and os.path.exists(temp):
                    os.unlink(temp)
            self.efforts = updated
            self.touched.add(sid)
            return level

    def bind(self, sid, level):
        """新会话拿到 sid 后关联首轮选择，不覆盖随后手动保存的选择。"""
        with self.lock:
            if sid not in self.efforts and sid not in self.touched and level is not None:
                self._locked_save(sid, level)
```

注意：`set` 里落盘逻辑与 `ModelPreferences._save` 相同但 `touched.add` 语义并入 `set`（`ApprovalPreferences.set` 不维护 touched，effort 的 bind 语义照 `ModelPreferences`）。若想避免 `set`/`bind` 重复落盘代码，可像 `ModelPreferences` 那样拆一个 `_locked_save(sid, level)` 私有方法（持有锁调用）：`set` 校验后调 `_locked_save` 并 `touched.add`；`bind` 直接调 `_locked_save`。实现时二选一，保持与 `ModelPreferences` 风格一致即可。

- [ ] **Step 4: 运行确认通过**

```
PYTHONPATH=. uv run python -m unittest tests.test_session_preferences -v
```
预期：PASS（全部用例，含原有）。

- [ ] **Step 5: 提交**

```
git add session_preferences.py tests/test_session_preferences.py
git commit -m "feat: 新增 EffortPreferences 与 effort_args 参数拼装"
```

---

### Task 2: Hosted 接入 effort（spawn、偏好、bind）

**Files:**
- Modify: `monitor.py`（import、`Monitor.__init__`、`host_session`/`_host_session_locked`、`Hosted.__init__`、`Hosted.start`、`Hosted._on_init`）
- Test: `tests/test_model_execution.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_model_execution.py` 的 `ExecutionTests` 类里追加（`start_host` 已有的模式之上）：

```python
    def test_hosted_effort_spawn_argument(self):
        captured = []
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **kwargs):
            captured.append(cmd)
            return original([sys.executable, fake] + cmd[1:], **kwargs)
        with patch.object(monitor.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), "existing",
                                                   "hi", effort="high")
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        self.assertIn("--effort=high", captured[0])
        self.assertEqual(self.mon.efforts.get("existing"), "high")
        self.wait_done(response["job_id"])

    def test_hosted_effort_default_uses_service_args(self):
        captured = []
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **kwargs):
            captured.append(cmd)
            return original([sys.executable, fake] + cmd[1:], **kwargs)
        with patch.object(monitor.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), "existing", "hi")
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        self.assertFalse(any(a.startswith("--effort") for a in captured[0]))
        self.wait_done(response["job_id"])

    def test_hosted_effort_invalid_rejected(self):
        code, response = self.mon.host_session(str(self.root), "existing", "hi",
                                               effort="ultra")
        self.assertEqual(code, 400)
        self.assertIn("effort", response["error"])
        self.assertIsNone(self.mon.efforts.get("existing"))

    def test_hosted_effort_binds_new_session(self):
        original = subprocess.Popen
        fake = str(Path(__file__).with_name("fake_claude.py"))
        def popen(cmd, **kwargs):
            return original([sys.executable, fake] + cmd[1:], **kwargs)
        with patch.object(monitor.subprocess, "Popen", side_effect=popen):
            code, response = self.mon.host_session(str(self.root), None, "hi",
                                                   effort="low")
        self.assertEqual(code, 202)
        host = self.mon.hosted[response["hosted_id"]]
        self.hosts.append(host)
        self.wait_done(response["job_id"])
        # fake_claude 的 init 携带 fake-session；bind 不覆盖已有，这里应写入
        self.assertEqual(self.mon.efforts.get("fake-session"), "low")
```

- [ ] **Step 2: 运行确认失败**

```
PYTHONPATH=. uv run python -m unittest tests.test_model_execution -v
```
预期：ERROR，`host_session() got an unexpected keyword argument 'effort'`。

- [ ] **Step 3: 实现**

`monitor.py`：

1. import 扩展（line 94-96）：

```python
from session_preferences import (APPROVAL_MODES, ApprovalPreferences, EFFORT_LEVELS,
                                 EffortPreferences, ModelPreferences,
                                 SessionMetadata, validate_metadata_patch,
                                 UNSET, default_model, effort_args, model_args,
                                 validate_effort, validate_model)
```

2. `Monitor.__init__`（line 406 `self.approvals = ...` 之后）：

```python
        self.efforts = EffortPreferences(CLAUDE_DIR / "claude-sessions" / "efforts.json")
```

3. `host_session` / `_host_session_locked`（line 776、788）签名加 `effort=UNSET`，透传：

```python
    def host_session(self, cwd, resume_id, text, force=False, model=UNSET,
                     approval=None, effort=UNSET):
        ...
            return self._host_session_locked(cwd, resume_id, text, force, model,
                                             approval, effort)

    def _host_session_locked(self, cwd, resume_id, text, force=False, model=UNSET,
                             approval=None, effort=UNSET):
```

`_host_session_locked` 内，model 校验块之后加 effort 校验：

```python
        if effort is not UNSET:
            try:
                selected_effort = validate_effort(effort)
            except ValueError as e:
                return 400, {"error": str(e)}
        else:
            selected_effort = self.efforts.get(resume_id) if resume_id else None
```

`if model is not UNSET and resume_id:` 持久化块之后加：

```python
        if effort is not UNSET and resume_id:
            try:
                self.efforts.set(resume_id, selected_effort)
            except ValueError as e:
                return 500, {"error": str(e)}
```

`Hosted(...)` 构造（line 819）加参数：

```python
        h = Hosted(self, cwd, resume_id, selected,
                   approval=approval or (self.approvals.get(resume_id) if resume_id else "auto"),
                   effort=selected_effort)
```

4. `Hosted.__init__`（line 1466）签名加 `effort=None`，属性区（`self.selected_model` 附近）加：

```python
        self.effort = effort if effort in EFFORT_LEVELS else None
        self.process_effort = None    # 常驻进程当前档位（spawn 或在线切换后）
```

5. `Hosted.start`（line 1552）：

```python
        cmd += effort_args(model_args(spawn_args, self.selected_model), self.effort)
```

Popen 成功、`self.status = "running"` 之后加：

```python
        self.process_effort = self.effort
```

6. `Hosted._on_init`（line 1803-1807）model bind 之后加：

```python
        if self.effort is not None:
            try:
                self.mon.efforts.bind(sid, self.effort)
            except ValueError:
                pass
```

- [ ] **Step 4: 运行确认通过**

```
PYTHONPATH=. uv run python -m unittest tests.test_model_execution -v
```
预期：PASS。

- [ ] **Step 5: 提交**

```
git add monitor.py tests/test_model_execution.py
git commit -m "feat: 托管进程 spawn 支持 --effort 并按会话持久化"
```

---

### Task 3: set_effort 在线切换与 HTTP 接口

**Files:**
- Modify: `monitor.py`（`Hosted.set_effort`、`Monitor.set_effort`、POST 路由、`/hosted` body、detail `effort` 字段）
- Test: `tests/test_model_execution.py`、`tests/test_management_api.py`

- [ ] **Step 1: 写失败测试**

`tests/test_model_execution.py` 的 `ExecutionTests` 追加：

```python
    def test_set_effort_switches_live_host_and_persists(self):
        host, _ = self.start_host()
        code, resp = self.mon.set_effort("existing", "high")
        self.assertEqual(code, 200)
        self.assertEqual(resp, {"effort": "high"})
        self.assertEqual(host.process_effort, "high")
        self.assertEqual(self.mon.efforts.get("existing"), "high")
        # default 清除偏好并回落模型默认
        code, resp = self.mon.set_effort("existing", "default")
        self.assertEqual(code, 200)
        self.assertEqual(resp, {"effort": "default"})
        self.assertIsNone(host.process_effort)
        self.assertIsNone(self.mon.efforts.get("existing"))

    def test_set_effort_invalid_value(self):
        code, resp = self.mon.set_effort("existing", "ultra")
        self.assertEqual(code, 400)
        self.assertIn("effort", resp["error"])

    def test_set_effort_without_host_only_persists(self):
        code, resp = self.mon.set_effort("existing", "medium")
        self.assertEqual(code, 200)
        self.assertEqual(self.mon.efforts.get("existing"), "medium")

    def test_set_effort_control_failure_keeps_preference(self):
        host, _ = self.start_host()
        with patch.object(host, "_control",
                          side_effect=RuntimeError("控制请求失败: apply_flag_settings")):
            code, resp = self.mon.set_effort("existing", "low")
        self.assertEqual(code, 200)
        self.assertTrue(resp["warning"])
        self.assertEqual(self.mon.efforts.get("existing"), "low")
```

`tests/test_management_api.py` 的 `ManagementAPITests` 追加（沿用 `self.request`）：

```python
    def test_effort_endpoint_persists_and_validates(self):
        headers = {'Content-Type': 'application/json'}
        result = json.load(self.request('/sessions/sid-1/effort',
                                        {'effort': 'high'}, headers))
        self.assertEqual(result, {'effort': 'high'})
        self.assertEqual(self.mon.efforts.get('sid-1'), 'high')
        detail = json.load(self.request('/sessions/sid-1'))
        self.assertEqual(detail['effort'], 'high')
        self.assertEqual(detail['commands'], [])
        try:
            self.request('/sessions/sid-1/effort', {'effort': 'ultra'}, headers)
            self.fail('应返回 400')
        except HTTPError as e:
            self.assertEqual(e.code, 400)
        # default 清除
        result = json.load(self.request('/sessions/sid-1/effort',
                                        {'effort': 'default'}, headers))
        self.assertEqual(result, {'effort': 'default'})
        self.assertIsNone(self.mon.efforts.get('sid-1'))
```

- [ ] **Step 2: 运行确认失败**

```
PYTHONPATH=. uv run python -m unittest tests.test_model_execution tests.test_management_api -v
```
预期：ERROR `Monitor has no attribute set_effort`；HTTP 测试 404。

- [ ] **Step 3: 实现**

`monitor.py`：

1. `Hosted` 里 `set_approval`（line 1981）之后加：

```python
    def set_effort(self, level):
        """在线切换思考档位；None 回落模型默认。控制失败抛错，偏好由调用方处理。"""
        if level is not None and level not in EFFORT_LEVELS:
            raise ValueError("invalid effort level")
        if self.alive():
            self._control("apply_flag_settings", settings={"effortLevel": level})
            self.process_effort = level
        self.effort = level
```

2. `Monitor.set_approval`（line 876）之后加：

```python
    def set_effort(self, sid, level):
        """保存思考档位；托管中先在线切换，失败不回滚保存（重托管后生效）。"""
        try:
            normalized = validate_effort(level)
        except ValueError as e:
            return 400, {"error": str(e)}
        with self.lock:
            h = self.hosted_by_sid.get(sid)
        warning = None
        if h and h.alive():
            try:
                h.set_effort(normalized)
            except (RuntimeError, TimeoutError, ValueError) as e:
                warning = f"在线切换失败（已保存，重托管后生效）: {e}"
        try:
            self.efforts.set(sid, normalized)
        except ValueError as e:
            return 500, {"error": str(e)}
        resp = {"effort": normalized or "default"}
        if warning:
            resp["warning"] = warning
        return 200, resp
```

3. POST 路由：approval 路由（line 2794-2800）之后加：

```python
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "effort":
                if not self._safe_json_write():
                    return
                body = self._read_body()
                level = body.get("effort") if isinstance(body, dict) else None
                code, resp = mon.set_effort(parts[1], level)
                return self._json(resp, code)
```

4. `/hosted` body（line 2726-2728）传 effort：

```python
                code, resp = mon.host_session(body.get("cwd"), body.get("resume_id"),
                                              text, bool(body.get("force")),
                                              body.get("model", UNSET), body.get("approval"),
                                              body.get("effort", UNSET))
```

5. detail（line 2567-2568 `d["selected_model"]`/`d["approval"]` 处）加：

```python
                d["effort"] = mon.efforts.get(sid)
```

（`commands` 字段在 Task 4 加。）

- [ ] **Step 4: 运行确认通过**

```
PYTHONPATH=. uv run python -m unittest tests.test_model_execution tests.test_management_api -v
```
预期：PASS。fake_claude 对未知 control 子类型统一回 success（`fake_claude.py` line 63），`apply_flag_settings` 无需改 fake。

- [ ] **Step 5: 提交**

```
git add monitor.py tests/test_model_execution.py tests/test_management_api.py
git commit -m "feat: 托管会话 effort 在线切换接口与持久化"
```

---

### Task 4: _on_init 捕获 slash_commands，detail 暴露 commands

**Files:**
- Modify: `monitor.py`（`Hosted.__init__` 加 `self.commands`、`_read_out` init 分支、`_on_init`、detail）、`tests/fake_claude.py`
- Test: `tests/test_model_execution.py`

- [ ] **Step 1: 写失败测试**

`tests/test_model_execution.py` 的 `ExecutionTests` 追加：

```python
    def test_hosted_commands_from_init(self):
        host, job_id = self.start_host()
        self.wait_done(job_id)
        deadline = time.time() + 2
        while not host.commands and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(host.commands, ["compact", "model", "review"])
```

- [ ] **Step 2: fake_claude 广播 slash_commands**

`tests/fake_claude.py` line 72 的 init 行改为：

```python
        emit({"type": "system", "subtype": "init", "session_id": "fake-session",
              "slash_commands": ["/compact", "model", "/review", "/model"]})
```

（含重复与不带 `/` 前缀的项，覆盖去重与前缀剥离。）

- [ ] **Step 3: 运行确认失败**

```
PYTHONPATH=. uv run python -m unittest tests.test_model_execution -v
```
预期：新用例 FAIL（`host.commands` 为 `[]` 或属性不存在）。

- [ ] **Step 4: 实现**

`monitor.py`：

1. `Hosted.__init__` 属性区加：

```python
        self.commands = []           # system/init 广播的指令目录（去 / 前缀、排序）
```

2. `_read_out` 的 init 分支（line 1711-1712）：

```python
            elif typ == "system" and obj.get("subtype") == "init":
                self._on_init(obj.get("session_id"), obj)
```

3. `_on_init`（line 1782）签名与目录捕获：

```python
    def _on_init(self, sid, init_obj=None):
        if not sid:
            return
        names, seen = [], set()
        for item in (init_obj or {}).get("slash_commands") or []:
            if not isinstance(item, str):
                continue
            name = item.strip().lstrip("/")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
```

方法体开头（`with self.ready:` 之前）插入 `self.commands = sorted(names)`？——不行，`commands` 会在 `ready` 锁外写、读取方在 detail 线程读。放进已有的 `with self.ready:` 块顶部：

```python
        with self.ready:
            self.commands = sorted(names)
            old = self.session_id
            ...
```

4. detail（line 2571 `h = mon.hosted_by_sid.get(sid)` 已存在）之后、`d["hosted"]` 附近加：

```python
                d["commands"] = list(h.commands) if h and h.alive() else []
```

- [ ] **Step 5: 运行确认通过（含全量后端）**

```
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```
预期：全部 PASS。

- [ ] **Step 6: 提交**

```
git add monitor.py tests/fake_claude.py tests/test_model_execution.py
git commit -m "feat: 托管进程捕获 slash_commands 并在 detail 暴露指令目录"
```

---

### Task 5: 前端 effort 下拉

**Files:**
- Modify: `templates/index.html`（`composerHtml`、`bindSendBox`、`bindNewBox`、`updateHostedControls`）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 写失败测试**

`tests/test_frontend_controls.js` 追加（沿用 `boot`/`response` 工具；参考现有 `bindSendBox("sid", {})` 的调用方式）：

```javascript
test('effort 下拉即切并同步服务端值', async () => {
  const requests=[];
  const b=boot(async(url, opts={})=>{
    requests.push([url, opts.body]);
    if(url==='/sessions/sid/effort') return response({effort:'high'});
    return response({effort:'high', approval:'auto', commands:[]});
  });
  b.run('bindSendBox("sid", {})');
  const sel=b.node('effortsel');
  sel.value='high';
  sel.dispatchEvent({type:'change'});
  await new Promise(r=>setImmediate(r));
  assert.deepEqual(requests.find(r=>r[0]==='/sessions/sid/effort'),
    ['/sessions/sid/effort', JSON.stringify({effort:'high'})]);
  // 软同步：detail 带 effort 时回填下拉
  b.run('updateHostedControls({effort:"low", approval:"auto"})');
  assert.equal(sel.value, 'low');
});
```

- [ ] **Step 2: 运行确认失败**

```
node --test tests/test_frontend_controls.js
```
预期：新用例 FAIL（`effortsel` 不存在 / onchange 无请求）。

- [ ] **Step 3: 实现**

`templates/index.html`：

1. `composerHtml`（line 549 审批 label 之后）加：

```html
      <label class="ctl" id="effortwrap" title="托管进程的思考档位；未托管时保存后下次托管生效">思考
        <select id="effortsel" aria-label="思考档位">
          <option value="default">默认</option>
          <option value="low">low</option>
          <option value="medium">medium</option>
          <option value="high">high</option>
        </select></label>
```

2. `updateHostedControls`（line 1305-1307 审批软同步之后）加：

```javascript
  const effortSel = document.getElementById("effortsel");
  if(effortSel && d && d.effort) effortSel.value = d.effort;
  sessionCommands = d && Array.isArray(d.commands) ? d.commands : [];
```

并在脚本顶部状态变量区（`let detailRuntime` 一类附近）声明：

```javascript
let sessionCommands = [];   // 当前会话的指令目录（仅托管进程活着时有内容）
```

3. `bindSendBox`：审批 onchange（line 1880-1893）之后加：

```javascript
  const effortSel = document.getElementById("effortsel");
  if(effortSel) effortSel.onchange = async () => {
    try{
      const r = await fetchT("/sessions/" + encodeURIComponent(id) + "/effort", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({effort: effortSel.value}),
      });
      const j = await r.json();
      document.getElementById("sendst").textContent =
        r.ok ? (j.warning ? ("思考档位已保存（" + j.warning + "）") : "思考档位已更新")
             : ("思考档位失败: " + (j.error || r.status));
    }catch(e){
      document.getElementById("sendst").textContent = "思考档位保存失败: " + e;
    }
  };
```

4. `bindNewBox`（line 1722）`bindComposerKeys()` 之后加禁用联动：

```javascript
  const hostcb = document.getElementById("hostcb"), newEffortSel = document.getElementById("effortsel");
  if(hostcb && newEffortSel){
    const syncEffort = () => { newEffortSel.disabled = !hostcb.checked; };
    hostcb.addEventListener("change", syncEffort);
    syncEffort();
  }
```

`btn.onclick` 里读取（`const approval = ...` 之后）：

```javascript
    const effort = newEffortSel && !newEffortSel.disabled ? newEffortSel.value : "default";
```

`/hosted` body（line 1774-1776）改为：

```javascript
        body: JSON.stringify(useHost
          ? {cwd: p.cwd, text, model: selectedModel, approval, effort}
          : {cwd: p.cwd, text, model: selectedModel}),
```

- [ ] **Step 4: 运行确认通过**

```
node --test tests/test_frontend_controls.js
```
预期：PASS（含原有）。

- [ ] **Step 5: 提交**

```
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 发送区增加思考档位下拉并接入 effort 接口"
```

---

### Task 6: 前端指令提示面板

**Files:**
- Modify: `templates/index.html`（纯函数、`wireCommandHints`、`composerHtml`、CSS、`bindSendBox`/`bindNewBox` 接线）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 写失败测试**

`tests/test_frontend_controls.js` 追加：

```javascript
test('指令 token 提取与前缀过滤', () => {
  const b=boot();
  assert.equal(b.run('commandToken("/mod")'), '/mod');
  assert.equal(b.run('commandToken("/model sonnet")'), null);
  assert.equal(b.run('commandToken("hi /model")'), null);
  assert.equal(b.run('commandToken("")'), null);
  assert.deepEqual(b.run('filterCommands(["model","compact","review"], "/m")'), ["model"]);
  assert.deepEqual(b.run('filterCommands(["model"], "/M")'), []);
  assert.deepEqual(b.run('filterCommands(["model"], null)'), []);
});

test('指令提示面板键盘补全，Enter 不拦截', () => {
  const b=boot();
  b.run('wireCommandHints(document.getElementById("msgbox"), () => ["model","compact"])');
  const box=b.node('msgbox'), panel=b.node('cmdhints');
  box.value='/m';
  box.dispatchEvent({type:'input'});
  assert.equal(panel.hidden, false);
  const down={type:'keydown',key:'ArrowDown',preventDefault(){}};
  box.dispatchEvent(down);   // 选中第二项 compact
  const tab={type:'keydown',key:'Tab',preventDefault(){}};
  box.dispatchEvent(tab);
  assert.equal(box.value, '/compact ');
  assert.equal(panel.hidden, true);
  // 目录为空不弹面板
  b.run('wireCommandHints(document.getElementById("msgbox"), () => [])');
  box.value='/x';
  box.dispatchEvent({type:'input'});
  assert.equal(panel.hidden, true);
});
```

注意：boot 的 mock node 没有 `replaceChildren` 之外的子节点 API；`wireCommandHints` 渲染用 `document.createElement` + `panel.replaceChildren(...)`/`panel.appendChild`，mock 已提供（`createElement` 返回带 `textContent`/`dataset` 的对象，`onclick` 直接赋值即可）。若 mock 缺少需要的方法（如 `focus`），在测试里给对应 node 补方法，而不是改产品代码绕过。

- [ ] **Step 2: 运行确认失败**

```
node --test tests/test_frontend_controls.js
```
预期：FAIL，`commandToken is not defined`。

- [ ] **Step 3: 实现**

`templates/index.html`：

1. 纯函数（`composerHtml` 之前）：

```javascript
function commandToken(value){
  const m = (value || "").match(/^\/[^\s]*$/);
  return m ? m[0] : null;
}
function filterCommands(commands, token){
  if(token == null) return [];
  return (commands || []).filter(name => ("/" + name).startsWith(token));
}
```

2. 面板接线（纯函数之后）：

```javascript
function wireCommandHints(box, getCommands){
  if(!box) return;
  const panel = document.getElementById("cmdhints");
  if(!panel) return;
  let selected = 0;
  const close = () => { panel.hidden = true; panel.replaceChildren(); selected = 0; };
  const render = () => {
    const token = commandToken(box.value);
    const found = filterCommands(getCommands(), token);
    if(token == null || !found.length){ close(); return; }
    if(selected >= found.length) selected = found.length - 1;
    panel.replaceChildren(...found.map((name, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cmd-hint" + (i === selected ? " selected" : "");
      btn.textContent = "/" + name;
      btn.onclick = () => { box.value = "/" + name + " "; close(); box.focus(); };
      return btn;
    }));
    panel.hidden = false;
  };
  box.addEventListener("input", () => { selected = 0; render(); });
  box.addEventListener("keydown", event => {
    if(panel.hidden) return;
    const found = filterCommands(getCommands(), commandToken(box.value));
    if(!found.length) return;
    if(event.key === "ArrowDown"){
      event.preventDefault(); selected = Math.min(selected + 1, found.length - 1); render();
    }else if(event.key === "ArrowUp"){
      event.preventDefault(); selected = Math.max(selected - 1, 0); render();
    }else if(event.key === "Tab"){
      event.preventDefault(); box.value = "/" + found[selected] + " "; close();
    }else if(event.key === "Escape"){
      close();
    }
  });
  box.addEventListener("blur", () => setTimeout(close, 150));
}
```

（Enter 不处理：现有 `bindComposerKeys` 的 Enter 发送逻辑不被拦截，满足"就当消息发送"。）

3. `composerHtml`：textarea（line 545）之后、`composer-actions` 之前插入：

```html
    <div id="cmdhints" hidden></div>
```

4. 接线：`bindSendBox` 里 `bindComposerKeys()` 之后加：

```javascript
  wireCommandHints(document.getElementById("msgbox"),
    () => (view.session === id ? sessionCommands : []));
```

`bindNewBox` 里 `bindComposerKeys()` 之后加（新建页无目录）：

```javascript
  wireCommandHints(document.getElementById("msgbox"), () => []);
```

5. CSS（`.composer textarea` 规则附近）：

```css
#cmdhints{margin-top:4px;max-height:180px;overflow:auto;border:1px solid var(--bd);
  border-radius:6px;background:var(--bg);display:flex;flex-direction:column}
#cmdhints[hidden]{display:none}
.cmd-hint{text-align:left;background:none;border:none;color:var(--fg);padding:6px 10px;
  font:inherit;font-size:13px;cursor:pointer}
.cmd-hint.selected{background:var(--hover-bg,rgba(59,130,246,.15))}
```

（先看 `:root`/主题里现有的 hover 背景变量名，有就用真名替换 `--hover-bg`。）

- [ ] **Step 4: 运行确认通过**

```
node --test tests/test_frontend_controls.js
```
预期：PASS。

- [ ] **Step 5: 提交**

```
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 发送框斜杠指令提示面板"
```

---

### Task 7: 全量验证与文档更新

**Files:**
- Modify: `CLAUDE.md`、`docs/superpowers/specs/2026-10-25-hosted-effort-command-hints-design.md`

- [ ] **Step 1: 全量测试**

```
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```
预期：全部 PASS。真实冒烟（可选，会调用模型）：

```
CLAUDE_SESSIONS_SMOKE=1 uv run python tests/smoke_permission_live.py
```

- [ ] **Step 2: 更新 CLAUDE.md**

- 「数据流」第 3 条 HTTP 层：接口列表加 `POST /sessions/<id>/effort`（设置思考档位）。
- 「关键设计决策」托管审批条目附近补一条：

```markdown
- **思考档位（effort）**：仅托管会话；`POST /sessions/<id>/effort` 在线切换
  （control `apply_flag_settings` 的 `effortLevel`，`default` 传 null 回落模型默认），
  档位持久化在 `~/.claude/claude-sessions/efforts.json`，重托管 spawn 带 `--effort`。
  在线切换失败不回滚保存。托管 init 的 `slash_commands` 存入 Hosted，detail 返回
  `commands`；发送框 `/` 单 token 输入时浮出提示面板（↑↓/Tab 补全），Enter 直发不校验。
```

- 「前端」段落：发送区描述补"思考档位下拉（托管即时切换）与斜杠指令提示面板"。

- [ ] **Step 3: 修订 spec 与实现一致**

`docs/superpowers/specs/2026-10-25-hosted-effort-command-hints-design.md` HTTP 一节
"会话不存在 404" 改为 "与审批接口一致，不校验会话存在性"（实现跟随现有 `/approval` 语义）。

- [ ] **Step 4: 提交**

```
git add CLAUDE.md docs/superpowers/specs/2026-10-25-hosted-effort-command-hints-design.md
git commit -m "docs: effort 与指令提示的设计及架构说明"
```

---

## 自查

- **Spec 覆盖**：EffortPreferences（T1）、effort_args/spawn（T2）、set_effort 在线切换+失败保留偏好（T3）、HTTP/detail（T3）、/hosted body effort（T3）、bind（T2）、slash_commands 捕获与 detail（T4）、前端下拉（T5）、提示面板含键盘与显示条件（T6）、CLAUDE.md（T7）。spec 的"未托管改动只保存"由 `Monitor.set_effort` 无托管分支覆盖（T3 测试）。
- **占位符**：无 TBD/TODO；所有代码步骤给全量代码。
- **类型一致性**：`validate_effort` 返回 `None|"low"|"medium"|"high"`；`EffortPreferences.set(sid, level_or_None)`；`Hosted.set_effort(level_or_None)`；前端 select 值 `"default"` 仅在 HTTP 边界出现。
