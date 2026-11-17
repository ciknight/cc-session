# Prompt 模板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在会话页和新建会话页的发送框旁加入服务端持久化的常用提示词模板，支持搜索、插入、新建、编辑和删除。

**Architecture:** 新增 `claude_sessions/prompts.py` 负责模板 JSON 的加载、校验、原子保存；`Monitor` 持有实例；`http_api.py` 暴露 `/prompt-templates` REST 接口；`templates/index.html` 用 body 顶层浮层实现「模板」入口，复用现有 `#queuepop`/`#syspop` 的定位与关闭模式。

**Tech Stack:** Python 3 标准库、Jinja2、原生 JS（无构建步骤）、node:test、unittest。

**Spec:** `docs/superpowers/specs/2026-11-18-prompt-templates-design.md`

## Global Constraints

- 不引入新依赖；后端只用标准库，前端不引入框架/构建工具。
- 代码标识符用英文，界面文案、注释与文档用中文。
- `config` 常量、`util.now_ts` 时钟一律以模块属性访问（`config.X` / `util.now_ts()`），便于测试 patch。
- 模板文件写操作必须原子落盘（同目录临时文件 + fsync + `os.replace`）。
- POST 复用 `_safe_json_write`（同源 + JSON Content-Type）；DELETE 复用 `_safe_write_origin`。
- 未获用户明确指示前不要执行 `git commit` / `git push`。
- 单测命令：`PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v`；前端：`node --test tests/test_frontend_controls.js`。

## Review Focus

- **模板标题/正文中的 HTML 必须转义**：列表、编辑表单的 `value`/textarea 都经 `esc()`，任何 `<img>`、`<script>`、引号都不能变成 DOM。→ Task 3/4 测试。
- **损坏的 `prompt-templates.json` 不能拖垮看板**：加载失败只让模板接口报错，会话列表/SSE 照常。→ Task 1/2 测试。
- **并发写与重复 id**：保存失败不能发布内存状态；文件里重复 id 视为损坏。→ Task 1 测试。
- **插入边界**：光标越界、反向选区、空正文、模板正文首尾空白都要有确定行为。→ Task 3/4 测试。
- **数量与长度上限**：第 101 条、超长标题/正文必须在服务端被拒绝，客户端不因超大输入卡死。→ Task 1/2 测试。

---

### Task 1: 模板存储模块

**Files:**
- Create: `claude_sessions/prompts.py`
- Modify: `claude_sessions/config.py`（末尾追加 3 个常量）
- Create: `tests/test_prompt_templates.py`

**Interfaces:**
- Produces: `PromptTemplates(path)`，方法：
  - `list() -> list[dict]`（返回副本，按新建顺序，最新在前）
  - `create(title: str, text: str) -> dict`
  - `update(template_id: str, title: str, text: str) -> dict`
  - `delete(template_id: str) -> bool`
  - 异常：输入不合法 `ValueError`；id 不存在 `KeyError`；文件损坏/保存失败 `RuntimeError`
- Produces: `config.PROMPT_TEMPLATE_LIMIT = 100`、`config.PROMPT_TEMPLATE_TITLE_CHARS = 80`、`config.PROMPT_TEMPLATE_TEXT_CHARS = 20_000`

- [ ] **Step 1: 写失败测试**

在 `tests/test_prompt_templates.py` 写入存储层测试（先只放这些，HTTP 测试在 Task 2 追加）：

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_sessions import config, prompts


class PromptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "prompt-templates.json"
        self.limit_patch = patch.object(config, "PROMPT_TEMPLATE_LIMIT", 3)
        self.limit_patch.start()
        self.now_patch = patch.object(prompts.util, "now_ts", return_value=1000.0)
        self.now_patch.start()
        self.store = prompts.PromptTemplates(self.path)

    def tearDown(self):
        self.now_patch.stop()
        self.limit_patch.stop()
        self.temp.cleanup()

    def test_create_update_delete_and_reload(self):
        first = self.store.create("  修 bug  ", "  按仓库风格修复  ")
        self.assertEqual(first["title"], "修 bug")
        self.assertEqual(first["text"], "按仓库风格修复")
        self.assertEqual(first["created_at"], 1000.0)
        second = self.store.create("", "写测试\n覆盖边界")
        self.assertEqual(second["title"], "写测试")

        updated = self.store.update(first["id"], "", "新的正文")
        self.assertEqual(updated["title"], "新的正文")
        self.assertEqual(updated["created_at"], first["created_at"])
        self.assertTrue(self.store.delete(second["id"]))
        self.assertFalse(self.store.delete(second["id"]))
        with self.assertRaises(KeyError):
            self.store.update("missing", "x", "y")

        reloaded = prompts.PromptTemplates(self.path)
        self.assertEqual([t["id"] for t in reloaded.list()], [first["id"]])
        self.assertEqual(reloaded.list()[0]["text"], "新的正文")

    def test_validation_and_limits(self):
        for title, text in (("", "   "), ("x", ""), (123, "y")):
            with self.assertRaises(ValueError):
                self.store.create(title, text)
        with self.assertRaises(ValueError):
            self.store.create("x" * 81, "正文")
        with self.assertRaises(ValueError):
            self.store.create("x", "y" * 20001)

        derived = self.store.create(None, "标题行\n正文")
        self.assertEqual(derived["title"], "标题行")
        self.store.create("a", "1")
        self.store.create("b", "2")
        with self.assertRaises(ValueError):
            self.store.create("c", "3")

    def test_corrupt_file_blocks_mutations_and_keeps_other_features_alive(self):
        self.path.write_text("{not json", encoding="utf-8")
        broken = prompts.PromptTemplates(self.path)
        with self.assertRaises(RuntimeError):
            broken.list()
        with self.assertRaises(RuntimeError):
            broken.create("a", "b")

        duplicate = {
            "version": 1,
            "templates": [
                {"id": "same", "title": "a", "text": "1", "created_at": 1, "updated_at": 1},
                {"id": "same", "title": "b", "text": "2", "created_at": 2, "updated_at": 2},
            ],
        }
        self.path.write_text(json.dumps(duplicate), encoding="utf-8")
        self.assertIsNotNone(prompts.PromptTemplates(self.path).error)

    def test_save_failure_does_not_publish_in_memory_state(self):
        self.store.create("a", "1")
        with patch.object(prompts, "_atomic_json_dump", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                self.store.create("b", "2")
        self.assertEqual([t["title"] for t in self.store.list()], ["a"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_prompt_templates.py' -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'claude_sessions.prompts'`

- [ ] **Step 3: 实现 `claude_sessions/prompts.py`**

文件职责、校验规则与异常语义：

```python
"""常用提示词模板：服务端 JSON 持久化，供发送框一键插入。"""
import json
import secrets
import threading
from pathlib import Path

from session_preferences import _atomic_json_dump

from . import config, util

RECORD_KEYS = {"id", "title", "text", "created_at", "updated_at"}
LOAD_ERROR = "提示词模板文件无法读取或格式损坏，请先修复或移走该文件"
SAVE_ERROR = "提示词模板保存失败，请检查目录权限或磁盘空间"


def _title_from_text(text):
    return text.strip().splitlines()[0].strip()[:40] if text.strip() else ""


def _normalize(title, text):
    # title 允许 None/空串，自动取正文首行；正文必须是非空字符串
    ...


class PromptTemplates:
    def __init__(self, path):
        # 加载 {"version": 1, "templates": [...]}；任一记录非法/重复 id/超上限
        # 都整体不加载，self.error = LOAD_ERROR；FileNotFoundError 视为空列表
        ...

    def list(self):
        ...

    def create(self, title, text):
        ...

    def update(self, template_id, title, text):
        ...

    def delete(self, template_id):
        ...

    def _require_available(self):
        # self.error 存在时 raise RuntimeError(LOAD_ERROR)
        ...

    def _save(self, templates):
        # try: _atomic_json_dump(self.path, {"version": 1, "templates": templates},
        #                        "prompt-templates-")
        # except OSError as exc: raise RuntimeError(SAVE_ERROR) from exc
        ...
```

实现约束：

- `create`：持锁 → `_require_available()` → 数量上限检查 → `_normalize` → `id = secrets.token_hex(8)` → 新记录放列表头部 → `_save` 成功后才替换 `self.templates` → 返回 `dict(record)`。
- `update`：持锁 → `_require_available()` → 先 `_normalize` → 按 id 找到原记录（找不到 `raise KeyError(template_id)`）→ 保留 `created_at` 与列表位置，刷新 `updated_at` → 保存成功才发布 → 返回副本。
- `delete`：持锁 → `_require_available()` → 找到则保存删除后的列表并返回 `True`，否则返回 `False`。
- 加载校验：`version` 必须是 int 且等于 1；`templates` 必须是 list；每条 `set(record) == RECORD_KEYS`；`id` 非空 str 且不重复；`title`/`text` 非空 str 且分别不超过 `config.PROMPT_TEMPLATE_TITLE_CHARS` / `config.PROMPT_TEMPLATE_TEXT_CHARS`；`created_at`/`updated_at` 是 int/float 且非 bool；总条数不超过 `config.PROMPT_TEMPLATE_LIMIT`；任一失败置 `self.error = LOAD_ERROR`。
- `list()` 返回 `[dict(record) for record in self.templates]`，调用方不能改到内部状态。
- `_normalize`：`text` 必须是 str，strip 后非空且不超过 `config.PROMPT_TEMPLATE_TEXT_CHARS`；`title` 为 None 或 str，strip 后为空则 `_title_from_text(text)`，超过 `config.PROMPT_TEMPLATE_TITLE_CHARS` 抛 `ValueError`。

- [ ] **Step 4: 在 `config.py` 末尾追加常量**

```python
# 提示词模板：服务端全局共享，数量与单条长度设限，避免文件与浮层无限膨胀
PROMPT_TEMPLATE_LIMIT = 100
PROMPT_TEMPLATE_TITLE_CHARS = 80
PROMPT_TEMPLATE_TEXT_CHARS = 20_000
```

- [ ] **Step 5: 运行测试确认通过**

Run: `PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_prompt_templates.py' -v`
Expected: PASS（4 个测试）

---

### Task 2: Monitor 接入与 HTTP API

**Files:**
- Modify: `claude_sessions/core.py`（import 区 + `Monitor.__init__`）
- Modify: `claude_sessions/http_api.py`（`do_GET` / `do_POST` / `do_DELETE`）
- Modify: `tests/test_prompt_templates.py`（追加 HTTP 测试类）

**Interfaces:**
- Consumes: Task 1 的 `PromptTemplates` 与异常语义。
- Produces: `Monitor.prompts` 属性；接口：
  - `GET /prompt-templates` → `200 {"templates": [...]}`；损坏/保存失败 → `500 {"error": "..."}`
  - `POST /prompt-templates` body `{"id"?: str, "title"?: str, "text": str}` → `200 {"template": {...}}`；校验失败 400；id 不存在 404；损坏/保存失败 500
  - `DELETE /prompt-templates/<id>` → `200 {"ok": true}`；不存在 404；损坏/保存失败 500

- [ ] **Step 1: 写失败测试**

在 `tests/test_prompt_templates.py` 追加 HTTP 测试类（复用 `tests/test_management_api.py` 的服务器启动方式）：

```python
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import monitor


class PromptTemplateAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_patch = patch.object(config, "CLAUDE_DIR", self.root / ".claude")
        self.config_patch.start()
        self.mon = monitor.Monitor(600)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), monitor.make_handler(self.mon, 3))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.config_patch.stop()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        body = None if data is None else json.dumps(data).encode()
        req = Request(self.base + path, data=body,
                      headers=headers or {}, method="GET" if data is None else "POST")
        return urlopen(req)

    def test_crud_and_validation(self):
        headers = {"Content-Type": "application/json"}
        created = json.load(self.request("/prompt-templates",
                                         {"title": "", "text": "修 bug\n细节"}, headers))
        tid = created["template"]["id"]
        self.assertEqual(created["template"]["title"], "修 bug")
        self.assertEqual(json.load(self.request("/prompt-templates"))["templates"][0]["id"], tid)

        updated = json.load(self.request("/prompt-templates",
                                         {"id": tid, "title": "新标题", "text": "新正文"}, headers))
        self.assertEqual(updated["template"]["title"], "新标题")

        for payload in ({"text": ""}, {"text": 1}, {"id": tid, "text": ""}):
            with self.assertRaises(HTTPError) as error:
                self.request("/prompt-templates", payload, headers)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/prompt-templates", {"id": "missing", "text": "x"}, headers)
        self.assertEqual(error.exception.code, 404)

        req = Request(self.base + "/prompt-templates/" + tid, method="DELETE")
        self.assertEqual(json.load(urlopen(req)), {"ok": True})
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.base + "/prompt-templates/" + tid, method="DELETE"))
        self.assertEqual(error.exception.code, 404)

    def test_cross_site_post_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/prompt-templates", {"text": "x"},
                         {"Content-Type": "application/json",
                          "Origin": "http://evil.invalid"})
        self.assertEqual(error.exception.code, 403)
```

注意：`Request` 在 `data` 为 `None` 时是 GET；DELETE 在测试里显式构造（见上方 `req`）。

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_prompt_templates.py' -v`
Expected: FAIL，`404` / `AttributeError: 'Monitor' object has no attribute 'prompts'`

- [ ] **Step 3: 接入 Monitor**

`claude_sessions/core.py`：

```python
from .prompts import PromptTemplates
```

在 `self.metadata = ...` 之后加：

```python
self.prompts = PromptTemplates(
    config.CLAUDE_DIR / "claude-sessions" / "prompt-templates.json")
```

- [ ] **Step 4: 增加 HTTP 路由**

`claude_sessions/http_api.py` 的 `do_GET` 中，放在 `/interactions` 之后、`/file` 之前：

```python
if u.path == "/prompt-templates":
    try:
        return self._json({"templates": mon.prompts.list()})
    except RuntimeError as e:
        return self._json({"error": str(e)}, 500)
```

`do_POST` 中，放在 `_safe_json_write()` 之后、`/system/power` 之前：

```python
if u.path == "/prompt-templates":
    body = self._read_body()
    if not isinstance(body, dict):
        return self._json({"error": "invalid JSON body"}, 400)
    tid, title, text = body.get("id"), body.get("title", ""), body.get("text")
    if tid is not None and not isinstance(tid, str):
        return self._json({"error": '"id" 必须是字符串'}, 400)
    if not isinstance(title, str) or not isinstance(text, str):
        return self._json({"error": '"title" 和 "text" 必须是字符串'}, 400)
    try:
        record = (mon.prompts.update(tid, title, text) if tid
                  else mon.prompts.create(title, text))
    except KeyError:
        return self._json({"error": "模板不存在"}, 404)
    except ValueError as e:
        return self._json({"error": str(e)}, 400)
    except RuntimeError as e:
        return self._json({"error": str(e)}, 500)
    return self._json({"template": record})
```

`do_DELETE` 中，放在 `parts` 解构之后、`/hosted` 之前：

```python
if len(parts) == 2 and parts[0] == "prompt-templates":
    try:
        removed = mon.prompts.delete(parts[1])
    except RuntimeError as e:
        return self._json({"error": str(e)}, 500)
    return self._json({"ok": True}) if removed else self._json(
        {"error": "模板不存在"}, 404)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_prompt_templates.py' -v`
Expected: PASS（6 个测试）

---

### Task 3: 前端入口、样式与纯函数

**Files:**
- Modify: `templates/index.html`（CSS、`#tplpop` 容器、`composerHtml`、纯函数与常量）
- Modify: `tests/test_frontend_controls.js`（标记与纯函数测试）

**Interfaces:**
- Produces:
  - `templateTitleFromText(text: string) -> string`
  - `insertTemplateText(value: string, start: number, end: number, text: string) -> {value: string, cursor: number}`
  - `templateFilter(list: Array, query: string) -> Array`
  - `tplPopHtml() -> string`（Task 4 使用）
  - `#tplbtn` 按钮、`#tplpop` 容器

- [ ] **Step 1: 写失败测试**

在 `tests/test_frontend_controls.js` 追加：

```js
test('发送框提供模板入口，页面有顶层模板浮层', () => {
  const b = boot();
  for (const markup of [b.run('composerHtml("conv", {hosted:false})'),
                        b.run('composerHtml("new", {})')]) {
    assert.match(markup, /id="tplbtn"/);
    assert.match(markup, />模板</);
  }
  assert.match(html, /<div id="tplpop" hidden><\/div>/);
});

test('模板纯函数：标题、插入与过滤', () => {
  const b = boot();
  assert.equal(b.run('templateTitleFromText("  第一行  \\n第二行")'), '第一行');
  assert.equal(b.run('templateTitleFromText("x".repeat(50))').length, 40);
  assert.equal(b.run('templateTitleFromText("   ")'), '');

  const cases = [
    ['insertTemplateText("abc", 1, 1, "XY")', 'aXYbc', 3],
    ['insertTemplateText("abc", 1, 2, "Z")', 'aZc', 2],
    ['insertTemplateText("abc", 99, 99, "!")', 'abc!', 4],
    ['insertTemplateText("abc", 2, 1, "Z")', 'aZc', 2],
    ['insertTemplateText("", 0, 0, "hi")', 'hi', 2],
  ];
  for (const [expression, value, cursor] of cases) {
    const result = b.run(expression);
    assert.equal(result.value, value, expression);
    assert.equal(result.cursor, cursor, expression);
  }

  b.run('var tpls = [{title:"修 bug",text:"按仓库风格修复"},'
    + '{title:"写测试",text:"cover edge cases"}];');
  assert.equal(b.run('templateFilter(tpls, "BUG").length'), 1);
  assert.equal(b.run('templateFilter(tpls, "cover").length'), 1);
  assert.equal(b.run('templateFilter(tpls, "").length'), 2);
  assert.equal(b.run('templateFilter(tpls, "nomatch").length'), 0);
});

test('模板列表与编辑表单转义 HTML', () => {
  const b = boot();
  b.run('tplPop.list = [{id:"x",title:"<img src=x>",text:"<script>alert(1)</script>"}]');
  const list = b.run('tplPopHtml()');
  assert.doesNotMatch(list, /<img src=x>|<script>/);
  assert.match(list, /&lt;img src=x&gt;/);
  assert.match(list, /&lt;script&gt;/);

  b.run('tplPop.editing = {id:null,title:"<b>",text:"<i>"}');
  const form = b.run('tplPopHtml()');
  assert.doesNotMatch(form, /<b>|<i>/);
  assert.match(form, /value="&lt;b&gt;"/);
  assert.match(form, /&lt;i&gt;/);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test tests/test_frontend_controls.js`
Expected: FAIL，`tplbtn` 不存在 / `templateTitleFromText is not defined`

- [ ] **Step 3: 增加样式与容器**

在 `#syspop` 样式之后追加（具体选择器与设计稿一致即可）：

```css
#tplpop{position:fixed;z-index:200;width:min(92vw,460px);max-height:min(70vh,520px);
  overflow-y:auto;background:var(--bg);border:1px solid var(--bd);border-radius:8px;
  padding:10px 14px;box-shadow:0 8px 24px rgba(0,0,0,.16);text-align:left}
#tplpop[hidden]{display:none}
.tplhead{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px}
.tplsearch{width:100%;box-sizing:border-box;font:inherit;font-size:13px;padding:5px 8px;
  background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:6px;margin:4px 0 8px}
.tplitem{padding:8px 0;border-top:1px solid var(--bd)}
.tpltitle{font-weight:600;font-size:13px;cursor:pointer;overflow-wrap:anywhere}
.tpltext{font-size:12px;color:var(--mut);white-space:pre-wrap;overflow-wrap:anywhere;
  max-height:3.6em;overflow:hidden;margin:2px 0 4px;cursor:pointer}
.tplacts{display:flex;gap:6px}
.tplacts .back{font-size:12px;padding:2px 8px}
.tplform label{display:block;font-size:12px;color:var(--mut);margin:6px 0 3px}
.tplform input,.tplform textarea{width:100%;box-sizing:border-box;font:inherit;font-size:13px;
  padding:5px 8px;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:6px}
.tplform textarea{min-height:110px;resize:vertical}
.tplfoot{margin-top:8px;padding-top:8px;border-top:1px solid var(--bd);display:flex;gap:8px;flex-wrap:wrap}
```

在 `<div id="syspop" hidden></div>` 后追加：

```html
<div id="tplpop" hidden></div>
```

- [ ] **Step 4: 加按钮与纯函数**

`composerHtml` 的「图片」按钮后加：

```html
<button class="back" id="tplbtn" type="button"
        title="插入常用提示词模板">模板</button>
```

在队列浮层代码附近新增：

```js
/* ---------- 提示词模板：发送框旁「模板」浮层 ---------- */
const TPL_TITLE_MAX = 80, TPL_TEXT_MAX = 20000;
const tplPop = {open:false, loading:false, error:"", list:[], query:"",
                editing:null, formError:"", note:""};

function templateTitleFromText(text){
  const trimmed = String(text ?? "").trim();
  return trimmed ? trimmed.split(/\r?\n/)[0].trim().slice(0, 40) : "";
}
function insertTemplateText(value, start, end, text){
  const base = String(value ?? ""), insert = String(text ?? "");
  let a = Number.isFinite(start) ? Math.trunc(start) : base.length;
  let b = Number.isFinite(end) ? Math.trunc(end) : a;
  a = Math.max(0, Math.min(a, base.length));
  b = Math.max(0, Math.min(b, base.length));
  if(a > b){ const t = a; a = b; b = t; }
  return {value: base.slice(0, a) + insert + base.slice(b), cursor: a + insert.length};
}
function templateFilter(list, query){
  const q = String(query ?? "").trim().toLowerCase();
  if(!q) return (list || []).slice();
  return (list || []).filter(t =>
    (String(t.title ?? "") + "\n" + String(t.text ?? "")).toLowerCase().includes(q));
}
function tplItemHtml(t){
  const id = esc(t.id);
  return `<div class="tplitem"><div class="tpltitle" data-tpl-insert="${id}">${esc(t.title)}</div>
    <div class="tpltext" data-tpl-insert="${id}">${esc(t.text)}</div>
    <div class="tplacts"><button class="back" data-tpl-insert="${id}">插入</button>
      <button class="back" data-tpl-edit="${id}">编辑</button>
      <button class="back" data-tpl-delete="${id}">删除</button></div></div>`;
}
function tplListHtml(){
  const items = templateFilter(tplPop.list, tplPop.query);
  if(tplPop.loading) return '<div class="mut">加载中…</div>';
  if(!items.length) return `<div class="mut">${tplPop.list.length
    ? "没有匹配的模板" : "还没有模板，点「+ 新建」或把输入框里的文字存为模板"}</div>`;
  return items.map(tplItemHtml).join("");
}
function tplFormHtml(){
  const e = tplPop.editing;
  return `<div class="tplhead"><b>${e.id ? "编辑模板" : "新建模板"}</b></div>
    <div class="tplform"><label for="tpltitle">标题（留空自动取正文首行）</label>
      <input id="tpltitle" maxlength="${TPL_TITLE_MAX}" value="${esc(e.title)}">
      <label for="tpltext">正文</label>
      <textarea id="tpltext" maxlength="${TPL_TEXT_MAX}">${esc(e.text)}</textarea></div>
    ${tplPop.formError ? `<div class="terror" role="status">${esc(tplPop.formError)}</div>` : ""}
    <div class="tplfoot"><button class="back" data-tpl-save>保存</button>
      <button class="back" data-tpl-cancel>取消</button></div>`;
}
function tplPopHtml(){
  if(tplPop.editing) return tplFormHtml();
  const box = document.getElementById("msgbox");
  const hasText = !!(box && box.value && box.value.trim());
  return `<div class="tplhead"><b>提示词模板</b>
      <button class="back" data-tpl-new>+ 新建</button></div>
    <input class="tplsearch" id="tplsearch" type="search"
           placeholder="搜索标题或内容…" aria-label="搜索模板" value="${esc(tplPop.query)}">
    ${tplPop.error ? `<div class="terror" role="status">${esc(tplPop.error)}
      <button class="back" data-tpl-retry>重试</button></div>` : ""}
    ${tplPop.note ? `<div role="status">${esc(tplPop.note)}</div>` : ""}
    <div id="tpllist">${tplListHtml()}</div>
    ${hasText ? `<div class="tplfoot">
      <button class="back" data-tpl-save-current>保存当前输入为模板</button></div>` : ""}`;
}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `node --test tests/test_frontend_controls.js`
Expected: PASS（新增 3 个测试 + 原有测试全过）

---

### Task 4: 模板浮层交互

**Files:**
- Modify: `templates/index.html`（浮层状态函数与事件绑定）
- Modify: `tests/test_frontend_controls.js`（渲染与空状态测试）

**Interfaces:**
- Consumes: Task 2 的 `/prompt-templates` 接口；Task 3 的 `tplPopHtml`、`tplListHtml`、`insertTemplateText`、`templateTitleFromText`、`templateFilter`。
- Produces: `toggleTplPop(open)`、`loadPromptTemplates()`、`renderTplPop()`、`positionTplPop()`、`insertTemplateById(id)`、`openTemplateEditor(tpl)`、`openCurrentAsTemplate()`、`saveTemplateEditor()`、`deletePromptTemplate(id)`

- [ ] **Step 1: 写失败测试**

在 `tests/test_frontend_controls.js` 追加：

```js
test('模板浮层渲染搜索、空状态与保存当前输入入口', () => {
  const b = boot();
  b.run('tplPop.list = []; tplPop.query = ""; tplPop.editing = null;');
  assert.match(b.run('tplPopHtml()'), /还没有模板/);

  b.run('tplPop.list = [{id:"a",title:"修 bug",text:"修复"}]; tplPop.query = "nomatch";');
  assert.match(b.run('tplPopHtml()'), /没有匹配的模板/);
  assert.match(b.run('tplPopHtml()'), /id="tplsearch"/);

  b.run('tplPop.query = "";');
  b.node('msgbox').value = "当前草稿";
  assert.match(b.run('tplPopHtml()'), /data-tpl-save-current/);
  b.node('msgbox').value = "   ";
  assert.doesNotMatch(b.run('tplPopHtml()'), /data-tpl-save-current/);
});

test('模板编辑状态优先于列表，摘要截断', () => {
  const b = boot();
  b.run('tplPop.editing = {id:"a",title:"t",text:"body"};');
  const form = b.run('tplPopHtml()');
  assert.match(form, /编辑模板/);
  assert.match(form, /id="tpltext"/);
  assert.doesNotMatch(form, /tplsearch/);
  b.run('tplPop.editing = null;');
  assert.match(b.run('tplPopHtml()'), /tplsearch/);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test tests/test_frontend_controls.js` 或先实现再跑
Expected: 若 Task 3 的 `tplPopHtml` 已存在则可能通过；这些测试主要在锁定 Task 4 不应破坏的渲染契约。若已通过，保留它们作为回归测试。

- [ ] **Step 3: 实现浮层函数**

在 Task 3 的纯函数之后追加：

```js
function positionTplPop(){
  const pop = document.getElementById("tplpop"), btn = document.getElementById("tplbtn");
  if(!pop || pop.hidden || !btn) return;
  const rect = btn.getBoundingClientRect && btn.getBoundingClientRect();
  const root = document.documentElement;
  const vw = root ? root.clientWidth : 0, vh = root ? root.clientHeight : 0;
  if(!rect || !vw || !vh) return;
  const w = pop.offsetWidth || 0, h = pop.offsetHeight || 0;
  let left = Math.min(Math.max(8, rect.right - w), vw - w - 8);
  let top = rect.top - h - 6;   // 输入区在底部，优先向上展开
  if(top < 8) top = Math.min(rect.bottom + 6, Math.max(8, vh - h - 8));
  pop.style.left = Math.round(left) + "px";
  pop.style.top = Math.round(top) + "px";
}
function renderTplPop(){
  const pop = document.getElementById("tplpop");
  if(!pop || !tplPop.open) return;
  pop.innerHTML = tplPopHtml();
  pop.hidden = false;
  positionTplPop();
}
function renderTplList(){
  const list = document.getElementById("tpllist");
  if(list) list.innerHTML = tplListHtml();
}
function toggleTplPop(open){
  const pop = document.getElementById("tplpop");
  if(!pop) return;
  tplPop.open = !!open;
  tplPop.editing = null; tplPop.query = "";
  tplPop.error = ""; tplPop.formError = ""; tplPop.note = "";
  if(!open){ pop.hidden = true; pop.innerHTML = ""; return; }
  renderTplPop();
  loadPromptTemplates();
}
async function loadPromptTemplates(){
  if(tplPop.loading) return;
  tplPop.loading = true; tplPop.error = "";
  renderTplPop();
  try{
    const r = await fetchT("/prompt-templates", {}, 15000);
    const j = await r.json().catch(() => ({}));
    if(!r.ok) throw Error(j.error || ("HTTP " + r.status));
    tplPop.list = Array.isArray(j.templates) ? j.templates : [];
  }catch(e){
    tplPop.error = "模板加载失败：" + (e.message || e);
  }
  tplPop.loading = false;
  if(tplPop.open) renderTplPop();
}
function insertTemplateById(id){
  const t = (tplPop.list || []).find(x => x.id === id);
  const box = document.getElementById("msgbox");
  if(!t || !box) return;
  const result = insertTemplateText(box.value, box.selectionStart, box.selectionEnd, t.text);
  box.value = result.value;
  if("selectionStart" in box) box.selectionStart = box.selectionEnd = result.cursor;
  if(box.focus) box.focus();
  if(box.oninput) box.oninput();
  autosizeComposer(box);
  toggleTplPop(false);
}
function openTemplateEditor(tpl, prefill){
  tplPop.editing = tpl
    ? {id:tpl.id, title:tpl.title || "", text:tpl.text || ""}
    : {id:null, title:"", text:String(prefill || "")};
  tplPop.formError = ""; tplPop.note = "";
  if(!tpl && tplPop.editing.text) tplPop.editing.title = templateTitleFromText(tplPop.editing.text);
  renderTplPop();
  const field = document.getElementById("tpltitle");
  if(field && field.focus) field.focus();
}
function openCurrentAsTemplate(){
  const box = document.getElementById("msgbox"), text = box ? box.value : "";
  if(String(text || "").trim()) openTemplateEditor(null, text);
}
async function saveTemplateEditor(){
  const e = tplPop.editing;
  if(!e) return;
  if(!String(e.text || "").trim()){
    tplPop.formError = "模板正文不能为空"; renderTplPop(); return;
  }
  const body = {title:e.title || "", text:e.text || ""};
  if(e.id) body.id = e.id;
  try{
    const r = await fetchT("/prompt-templates", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}, 15000);
    const j = await r.json().catch(() => ({}));
    if(!r.ok || !j.template) throw Error(j.error || ("HTTP " + r.status));
    const saved = j.template;
    const index = tplPop.list.findIndex(t => t.id === saved.id);
    if(index < 0) tplPop.list.unshift(saved);
    else tplPop.list[index] = saved;
    tplPop.editing = null; tplPop.formError = ""; tplPop.note = "已保存";
    renderTplPop();
  }catch(err){
    tplPop.formError = "保存失败：" + (err.message || err);
    renderTplPop();
  }
}
async function deletePromptTemplate(id){
  const tpl = (tplPop.list || []).find(x => x.id === id);
  if(!tpl || (window.confirm && !window.confirm(`删除模板「${tpl.title}」？`))) return;
  try{
    const r = await fetchT("/prompt-templates/" + encodeURIComponent(id),
                           {method:"DELETE"}, 15000);
    const j = await r.json().catch(() => ({}));
    if(!r.ok) throw Error(j.error || ("HTTP " + r.status));
    tplPop.list = tplPop.list.filter(x => x.id !== id);
    tplPop.note = "已删除"; renderTplPop();
  }catch(e){
    tplPop.error = "删除失败：" + (e.message || e);
    renderTplPop();
  }
}
```

- [ ] **Step 4: 绑定事件**

在 `#main` 的 click 委托里，队列 chip 分支之后加：

```js
const tplBtn = e.target.closest && e.target.closest("#tplbtn");
if(tplBtn){ e.preventDefault?.(); toggleTplPop(!tplPop.open); return; }
```

在文件末尾（系统面板事件附近）追加：

```js
document.getElementById("tplpop").addEventListener("click", e => {
  const insert = e.target.closest && e.target.closest("[data-tpl-insert]");
  if(insert){ insertTemplateById(insert.dataset.tplInsert); return; }
  const edit = e.target.closest && e.target.closest("[data-tpl-edit]");
  if(edit){ openTemplateEditor(tplPop.list.find(x => x.id === edit.dataset.tplEdit)); return; }
  const del = e.target.closest && e.target.closest("[data-tpl-delete]");
  if(del){ deletePromptTemplate(del.dataset.tplDelete); return; }
  if(e.target.closest && e.target.closest("[data-tpl-new]")){ openTemplateEditor(null); return; }
  if(e.target.closest && e.target.closest("[data-tpl-save-current]")){ openCurrentAsTemplate(); return; }
  if(e.target.closest && e.target.closest("[data-tpl-save]")){ saveTemplateEditor(); return; }
  if(e.target.closest && e.target.closest("[data-tpl-cancel]")){
    tplPop.editing = null; tplPop.formError = ""; renderTplPop(); return;
  }
  if(e.target.closest && e.target.closest("[data-tpl-retry]")){ loadPromptTemplates(); }
});
document.getElementById("tplpop").addEventListener("input", e => {
  if(!e.target) return;
  if(e.target.id === "tplsearch"){ tplPop.query = e.target.value; renderTplList(); return; }
  if(tplPop.editing && e.target.id === "tpltitle") tplPop.editing.title = e.target.value;
  if(tplPop.editing && e.target.id === "tpltext") tplPop.editing.text = e.target.value;
});
document.addEventListener("click", e => {
  if(!tplPop.open) return;
  const pop = document.getElementById("tplpop");
  if(pop && pop.contains && pop.contains(e.target)) return;
  if(e.target.closest && e.target.closest("#tplbtn")) return;
  toggleTplPop(false);
});
document.addEventListener("keydown", e => {
  if(e.key !== "Escape" || !tplPop.open) return;
  if(tplPop.editing){ tplPop.editing = null; tplPop.formError = ""; renderTplPop(); return; }
  toggleTplPop(false);
});
document.addEventListener("scroll", () => { if(tplPop.open) positionTplPop(); }, true);
if(typeof window !== "undefined" && window.addEventListener)
  window.addEventListener("resize", () => { if(tplPop.open) positionTplPop(); });
```

导航/面板重建时关闭浮层：

- `renderConversation` 的 `if(isNew){ ... }` 分支内、`main.innerHTML = ...` 之后加 `toggleTplPop(false);`。
- `renderNewSession` 在重建 `main.innerHTML` 之后加 `toggleTplPop(false);`。

- [ ] **Step 5: 运行前端测试**

Run: `node --test tests/test_frontend_controls.js`
Expected: PASS（全部测试）

- [ ] **Step 6: 手工验证**

Run: `uv run monitor.py --port 8765`
检查：
1. 会话页点「模板」→ 浮层出现在按钮上方；新建页同样可用。
2. 输入一段文字 → 点「保存当前输入为模板」→ 标题自动取首行 → 保存后列表出现。
3. 点模板正文 → 文本插入光标处；有选区时替换选区；输入框草稿版本和高度更新。
4. 搜索标题/正文；编辑、删除；刷新页面后模板仍在。
5. 手机宽度（≤768px）浮层不超出视口；Esc/外点可关闭。

---

### Task 5: 文档与全量验证

**Files:**
- Modify: `CLAUDE.md`（架构模块列表、数据流、前端说明）
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/superpowers/specs/2026-11-18-prompt-templates-design.md`（状态改为已实施）

- [ ] **Step 1: 更新 `CLAUDE.md`**

在架构模块列表补：

```text
`prompts.py` — 常用提示词模板的服务端 JSON 存储（原子写、数量/长度校验）
```

在数据流/HTTP 层补：

```text
`GET/POST /prompt-templates` 与 `DELETE /prompt-templates/<id>` 管理常用提示词模板，
存 `CLAUDE_DIR/claude-sessions/prompt-templates.json`；网页发送框「模板」浮层支持搜索、
插入光标处、保存当前输入、编辑与删除。
```

- [ ] **Step 2: 更新 README 功能列表**

`README.md` 在「Keep work moving」后加：

```markdown
- **Reuse prompts** — save prompt templates on the server and insert them into the composer from any device.
```

`README.zh-CN.md` 对应位置：

```markdown
- **复用常用提示词** —— 提示词模板保存在服务端，任意设备都能一键插入发送框。
```

- [ ] **Step 3: 更新设计稿状态**

把 `docs/superpowers/specs/2026-11-18-prompt-templates-design.md` 的
`状态：待实施` 改为 `状态：已实施（见 docs/superpowers/plans/2026-11-18-prompt-templates.md）`。

- [ ] **Step 4: 运行全量测试**

Run:

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```

Expected: 全部 PASS。

- [ ] **Step 5: 检查改动**

Run: `git status --short` 与 `git diff --stat`
Expected: 只包含本计划涉及的文件；没有临时文件、缓存或 `_tmp` 改动。

---

## Self-Review

- **Spec coverage:** 存储/校验/API/前端入口/搜索/插入/保存当前输入/编辑/删除/文档均有对应 Task。
- **非目标未实现:** 没有占位符、项目级模板、排序、导入导出。
- **Type consistency:** `PromptTemplates` 方法名与 HTTP/前端使用的字段一致；`tplPop.list` 记录字段为 `id/title/text`，与 API 返回一致。
- **Review Focus 覆盖:** XSS（Task 3/4 测试）、损坏文件（Task 1/2）、并发/重复 id（Task 1）、插入边界（Task 3/4）、上限（Task 1/2）。
- **提交:** 按系统约束不在本会话自动 commit。