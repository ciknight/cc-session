# 文件在线预览 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 会话消息里 AI 写的文件链接（相对/绝对路径）点击后在新标签预览本地文件（md 渲染、文本、图片），直接访问历史遗留的错误 `/p/...` URL 也能兜底跳预览。

**Architecture:** 后端新增 `GET /file?path=`（分类读取本地文件，`meta=1` 只探元信息）与 `GET /view`（渲染独立轻量预览页 `preview.html`，仅引本地 static 的 marked/purify）。前端在消息组 DOM 新建时把无 scheme 的 `<a href>` 按会话 cwd 重写为 `/view?path=...`；路由兜底把 `<已知项目>/s/<相对路径>` 形态的地址重定向到 `/view`。

**Tech Stack:** Python stdlib（http.server、jinja2 已有）、原生 JS、已有本地 marked.min.js / purify.min.js。无新依赖。

**Spec:** `docs/superpowers/specs/2026-11-16-file-preview-design.md`

## Global Constraints

- 代码与注释全部中文；不新增第三方依赖。
- 测试命令：`PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v`；前端 `node --test tests/test_frontend_controls.js`。
- 可调常量放 `config.py`，以 `config.X` 模块属性访问（测试可 patch）。
- 所有文件测试用 tempfile 临时目录，不碰真实 `~/.claude`。
- 提交信息末尾带 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## Review Focus

1. **GBK 编码的 md**（中文 Windows 常见）→ utf-8 严格解码失败必须回退 gb18030，不得乱码。测试：Task 1。
2. **路径含 `+` 或中文/空格**：`parse_qs` 会把 `+` 当空格 → `/file` 解析 query 前须 `u.query.replace("+", "%2B")`；前端重写须 `encodeURIComponent`。测试：Task 2（`+`）、Task 4（中文）。
3. **`../`、`./`、反斜杠、`file:///F:/…`** 的链接解析必须词法归一正确（spec 互链在子目录）。测试：Task 4。
4. **超大文本**（几十 MB 日志）→ 只读头部约 8 MB、截断到 200 万字符并附提示，不得整读。测试：Task 2。
5. **遗留错误 URL 直达**：`/p/<已知项目>/s/docs/a/b.md`（多段）与 `/p/<项目>/s/README.md`（被当成会话 ID）都要落到预览。测试：Task 4（routeFileUrl / looksLikeFileName）。

---

### Task 1: `fileview` 纯函数模块

**Files:**
- Create: `claude_sessions/fileview.py`
- Test: `tests/test_file_preview.py`

**Interfaces:**
- Produces:
  - `classify_file(path: Path) -> str`：返回 `"markdown" | "text" | "image" | "binary"`。
  - `decode_text(data: bytes) -> str`：utf-8 严格 → gb18030 严格 → utf-8 `errors="replace"`。
  - `TEXT_HEAD_BYTES = 8_000_100`（内容模式下最多读的字节上限，供 Task 2 使用）。

- [ ] **Step 1: 写失败测试**（`tests/test_file_preview.py`，纯函数部分）

```python
from claude_sessions import fileview

class ClassifyTests(unittest.TestCase):
    def test_markdown_text_image_binary(self):
        # 临时目录写空文件后断言 kind：a.md → markdown、b.py → text、
        # c.svg → text、d.png → image（用 assertEqual 逐一断言）
    def test_无扩展名按内容嗅探(self):
        # 写临时文件后断言：(tmp/"noext") 内容 "纯文本" → "text"；
        # (tmp/"blob") 内容 b"\x00\x01\x02" → "binary"
    def test_decode_utf8_gbk_replace(self):
        assert fileview.decode_text("中文".encode("utf-8")) == "中文"
        assert fileview.decode_text("中文".encode("gb18030")) == "中文"
        self.assertIsInstance(fileview.decode_text(b"\xff\xfe\x00abc"), str)  # 不抛异常即可
```

- [ ] **Step 2: 跑测试确认失败** — `PYTHONPATH=. uv run python -m unittest tests.test_file_preview -v`，期望 ImportError/ModuleNotFoundError。
- [ ] **Step 3: 实现 `claude_sessions/fileview.py`**

扩展名集合（小写比较）：
- `MD_EXTS = {".md", ".markdown"}` → markdown
- `IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}` → image（svg 不在 image，归 text）
- `TEXT_EXTS = {".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".html", ".htm", ".css", ".scss", ".less", ".xml", ".svg", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".rs", ".go", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".vue", ".svelte", ".diff", ".patch", ".env", ".gitignore", ".dockerignore", ".editorconfig", ".lock", ".mod", ".sum"}` → text
- 文件名命中 `TEXT_NAMES = {"makefile", "dockerfile", "license", "readme", "notice", "changes"}`（小写全名比较）→ text
- 其余扩展名或无扩展名：读前 8192 字节，含 `b"\x00"` → binary，否则 text。

- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交** — `git add claude_sessions/fileview.py tests/test_file_preview.py`，`feat: 文件预览分类与文本解码纯函数`

---

### Task 2: `/file` 端点

**Files:**
- Modify: `claude_sessions/config.py`（新增 `PREVIEW_TEXT_CHARS = 2_000_000`、`PREVIEW_IMAGE_LIMIT = 32 * 1024 * 1024`）
- Modify: `claude_sessions/http_api.py`（`do_GET` 顶部路由区新增两个分支 + 私有方法 `_file_endpoint`）
- Test: `tests/test_file_preview.py`（追加 HTTP 测试类）

**Interfaces:**
- Consumes: Task 1 的 `classify_file` / `decode_text` / `TEXT_HEAD_BYTES`。
- Produces（Task 3/5 依赖的 HTTP 契约）:
  - `GET /file?path=<abs>[&meta=1]`
  - `meta=1` → `{"ok": true, "path", "name", "size", "kind"}`（kind 同 Task 1 四值）；400 非绝对路径/是目录，404 不存在。
  - 内容模式：markdown/text → `text/plain; charset=utf-8`（超 `PREVIEW_TEXT_CHARS` 截断，末尾追加 `\n\n[已截断：原文件 N 字节，完整内容请用「原始内容」下载]`）；image → 原始字节 + 对应 MIME，超 `PREVIEW_IMAGE_LIMIT` → 413；binary → `application/octet-stream` + `Content-Disposition: attachment; filename*=UTF-8''<urlencode(name)>`。
  - Windows 绝对路径（盘符或 UNC）才接受；`os.path.abspath` 归一。

- [ ] **Step 1: 写失败测试**（HTTP 类，复用 `tests/test_management_api.py` 的 setUp 模式：临时 CLAUDE_DIR + Monitor + ThreadingHTTPServer + urlopen；在临时目录写真实文件）

测试方法与断言：
- `test_meta_与各类型内容`：md → 200 `text/plain` 且正文等于文件内容、meta 返回 kind=markdown；png → Content-Type `image/png`。
- `test_加号与中文路径`：文件名 `a+b 中文.md` → 请求 `/file?path=` + quote 后 200。（服务端 `u.query.replace("+", "%2B")` 后再 `parse_qs`）
- `test_超大文本截断`：写 3_000_000 字符文件 → 响应正文 < 原文且含 `[已截断`。
- `test_错误路径`：相对路径 → 400；不存在 → 404；目录 → 400。

- [ ] **Step 2: 跑测试确认失败**（404 not found）
- [ ] **Step 3: 实现**：`config.py` 加常量；`http_api.py` 在 `do_GET` 的 `/interactions` 分支后加 `u.path == "/file"` 分支调 `self._file_endpoint(u)`；`_file_endpoint` 约 40 行：query 加号保护 → 取 path → `os.path.isabs`/`isfile` 校验 → `classify_file` → meta 或内容响应（文本只读前 `TEXT_HEAD_BYTES` 字节，decode 后 `[:config.PREVIEW_TEXT_CHARS]` 截断加标记）。
- [ ] **Step 4: 跑全部后端测试确认通过**（`tests.test_file_preview` + `tests.test_management_api` 不回归）
- [ ] **Step 5: 提交** — `feat: /file 端点按类型返回本地文件内容与元信息`

---

### Task 3: `/view` 预览页

**Files:**
- Create: `templates/preview.html`
- Modify: `claude_sessions/http_api.py`（`do_GET` 加 `u.path == "/view"` → 渲染 `preview.html`，无参数校验，错误由页面 JS 呈现）
- Test: `tests/test_file_preview.py`（追加）

**Interfaces:**
- Consumes: `/file` 两种模式（Task 2）；`/static/marked.min.js`、`/static/purify.min.js`。
- Produces: `/view?path=<abs>` → 200 HTML 预览壳（id 标记 `pv-shell`）。

- [ ] **Step 1: 写失败测试**：`GET /view` 与 `GET /view?path=x` → 200、`text/html`、正文含 `pv-shell` 与 `marked.min.js`。
- [ ] **Step 2: 确认失败**（404）
- [ ] **Step 3: 实现 `preview.html`**（单文件，约 150 行）：
  - 头部：文件名、完整路径、大小（人类可读）、类型 chip、「原始内容」链接（`/file?path=…`）。
  - JS：`path` 取自 `URLSearchParams`；先 `fetch('/file?path=…&meta=1')` → 按 kind 分支：markdown → fetch 文本 → `marked.parse` + `DOMPurify.sanitize` → 注入后重写页内 `<a href>`（无 scheme 的相对链接相对**被预览文件所在目录**词法解析为绝对路径 → `/view?path=…`，逻辑与 Task 4 的 `localPathFromHref` 相同但**独立内联实现**，预览页不引主 SPA 脚本）；text → `<pre>`（保留换行，不渲染 md）；image → `<img src="/file?path=…">`；binary → 提示不支持 + 下载链接。meta 非 200 → 居中错误文案。
  - 样式：约 30 行，字体/配色贴近看板，正文 `max-width: 860px` 居中。
  - `http_api.py` 路由：`_jinja.get_template("preview.html").render()`（无上下文变量）。
- [ ] **Step 4: 跑测试确认通过；用 `run` 技能起服务手点一次**（md/图片/不存在路径三种）
- [ ] **Step 5: 提交** — `feat: /view 独立预览页（md 渲染/文本/图片）`

---

### Task 4: index.html 链接重写纯函数

**Files:**
- Modify: `templates/index.html`（URL 路由区，`normPath` 之后追加四个函数）
- Test: `tests/test_frontend_controls.js`（追加）

**Interfaces:**
- Produces（Task 5 与 preview.html 语义对齐）:
  - `normLocalPath(p: string) -> string`：反斜杠→`/`，词法折叠 `.`/`..` 与重复斜杠（保留 UNC 前导 `//`），去尾斜杠。
  - `localPathFromHref(href: string, cwd: string) -> string | null`：`#…`/空 → null；scheme：`file:` → 去掉 `file:///` 前缀取本地路径，`http(s)/mailto/data/javascript` 等其他 → null；`//` 开头 → null；盘符（`^[A-Za-z]:[\\/]`）或 `\\\\` UNC → 归一后返回；其余（含 `/` 开头）→ `normLocalPath(cwd + "/" + href)`。
  - `viewFileUrl(p: string) -> string`：`"/view?path=" + encodeURIComponent(p)`。
  - `routeFileUrl(projects, full) -> string | null`：`projects` 为 `groupByProject()` 结果（元素含 `cwd`）；`normPath(full)` 等于某 `P/s/<相对路径>`（P 为已知项目 cwd）且相对路径非空 → `viewFileUrl(normLocalPath(P + "/" + 相对路径))`，否则 null。
  - `looksLikeFileName(s: string) -> bool`：`/\.[A-Za-z0-9]{1,10}$/.test(s)`——会话 ID（UUID / `parent::agent-hash`）永不带点号，结尾带扩展名的单段必是文件。

- [ ] **Step 1: 写失败测试**（`b.run()` 调纯函数，无需 DOM）：
  - `normLocalPath("F:\\a\\./b/../c//d/") === "F:/a/c/d"`
  - `localPathFromHref("docs/s/x.md", "F:/code/proj") === "F:/code/proj/docs/s/x.md"`；`("../x.md", "F:/code/proj") === "F:/code/x.md"`；`("F:\\code\\x.md", cwd) === "F:/code/x.md"`；`("file:///F:/code/x.md", cwd) === "F:/code/x.md"`；`("https://a.b/c", cwd) === null`；`("#anchor", cwd) === null`；`("中文 文档.md", "F:/code/proj") === "F:/code/proj/中文 文档.md"`
  - `routeFileUrl([{cwd:"F:/code/proj"}], "F:/code/proj/s/docs/a/b.md") === "/view?path=" + encodeURIComponent("F:/code/proj/docs/a/b.md")`；非 `s/` 前缀、未知项目 → null
  - `looksLikeFileName("README.md") === true`；`looksLikeFileName("a1b2c3d4-5678-90ab-cdef-1234567890ab") === false`；`looksLikeFileName("parent::agent-abc123") === false`
- [ ] **Step 2: 确认失败**（函数未定义）
- [ ] **Step 3: 实现四个函数**（纯字符串操作，不触磁盘；`..` 折叠越界即停在根）
- [ ] **Step 4: `node --test tests/test_frontend_controls.js` 通过（旧用例不回归）**
- [ ] **Step 5: 提交** — `feat: 会话链接→本地路径的解析纯函数`

---

### Task 5: index.html 接线（点击重写 + 路由兜底 + 单段文件判定）

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_frontend_controls.js`（纯函数已在 Task 4 覆盖）

**Interfaces:**
- Consumes: Task 4 全部函数（`rewriteConvLinks` 所需的 `localPathFromHref`/`viewFileUrl`，兜底所需的 `routeFileUrl`/`looksLikeFileName`/`normLocalPath`）。
- Produces:
  - `rewriteConvLinks(node, cwd)`：遍历 `node.querySelectorAll("a[href]")`，`localPathFromHref(href, cwd)` 命中 → `a.href = viewFileUrl(p)`、`target="_blank"`、`rel="noopener"`。无 `querySelectorAll`（vm 回退环境）时静默跳过。
  - 遗留 URL 单段形态的判定：会话 ID（UUID / `parent::agent-hash`）永不带点号，`looksLikeFileName(view.session)` 为真即文件，**同步判定、不发探测请求**。

- [ ] **Step 1: 无新增测试**——纯函数（`routeFileUrl`/`looksLikeFileName`/`localPathFromHref`）已在 Task 4 覆盖，本任务是三处 ≤ 8 行的同步接线。
- [ ] **Step 2: 实现接线**（三处，每处 ≤ 8 行）：
  1. `renderConvDom` 的 `if(!node){ node = makeNodeFromHtml(...); ... }` 分支内调 `rewriteConvLinks(node, (convState.detail || {}).cwd)`（cwd 空则跳过）。
  2. `renderMain` 中 `resolveViewProject(projects)` 之后、默认选项目分支之前：`const vu = view.session ? null : routeFileUrl(projects, view.fullProject || view.project); if(vu && location.replace) return location.replace(vu);`
  3. `renderMain` 的 `if(view.session){` 分支内、`renderConversation` 之前：`if(looksLikeFileName(view.session) && location.replace) return location.replace(viewFileUrl(normLocalPath(view.project + "/" + view.session)));`——`/p/<项目>/s/README.md` 这类单段文件 URL 直接跳预览，不再进入会话加载。
- [ ] **Step 3: `node --test` 全绿；`run` 技能起服务验证：含相对 md 链接的消息悬停显示 `/view?path=…`，中键新开可预览；贴历史错误 URL（多段与单段两种）自动跳预览**
- [ ] **Step 4: 提交** — `feat: 会话消息文件链接重写与遗留 URL 兜底跳预览`
- [ ] **Step 4: `node --test` 全绿；`run` 技能起服务验证：含相对 md 链接的消息悬停显示 `/view?path=…`，中键新开可预览；贴历史错误 URL 自动跳预览**
- [ ] **Step 5: 提交** — `feat: 会话消息文件链接重写与遗留 URL 兜底跳预览`

---

### Task 6: 文档更新与收尾

**Files:**
- Modify: `CLAUDE.md`（HTTP 层加一行 `/file`、`/view`；前端节一句话链接重写与兜底）
- Modify: `docs/superpowers/specs/2026-11-16-file-preview-design.md`（状态行改「已实施」）

- [ ] **Step 1: 更新两处文档**（与现有条目文风一致，各 1-3 行）
- [ ] **Step 2: 全量测试**：unittest discover + `node --test`，确认 0 失败
- [ ] **Step 3: 提交** — `docs: 文件在线预览的架构说明与设计状态`
