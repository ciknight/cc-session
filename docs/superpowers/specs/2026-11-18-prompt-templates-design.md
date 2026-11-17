# Prompt 模板 — 设计

日期：2026-11-18
状态：已实施（见 docs/superpowers/plans/2026-11-18-prompt-templates.md）

## 意图

看板已经能远程给会话发消息，但常用的长提示词（修 bug、写测试、代码审查）
每次都要重新输入或从别处粘贴。希望像 hapi 的 Scratchlist 一样，把常用
提示词存起来，在发送框旁一键插入；模板要跨设备共用，所以存服务端而不是
浏览器 localStorage。

参考项目（`_tmp`）：

- hapi `Scratchlist`：会话内保存文本草稿，一键放入输入框/队列；启发是
  「保存 → 点选插入」的轻交互，不引入复杂的模板变量系统。
- claudecodeui `QuickSettingsCommandsTab`：可搜索的命令/模板列表；启发是
  列表要有过滤，模板多时仍能快速找到。
- usher Agent 预设：命名预设集中管理；启发是模板带标题、服务端持久化。

## 目标 / 非目标

- 目标：新增/编辑/删除/搜索常用提示词模板；在会话页和新建会话页的发送框
  旁一键插入；跨浏览器和设备共享。
- 目标：插入到光标处（有选区时替换选区），不破坏当前草稿。
- 非目标：不做 `{cwd}`、`{session}` 等占位符替换；不做模板排序/分组；不做
  项目级模板；不做导入导出。
- 反目标：不改 Claude 的配置、slash 命令或转录；不把模板当作命令自动执行，
  它只是普通文本，插入后由用户自己发送。

## 方案对比

- **A（选定）：发送按钮旁「模板」按钮 + body 顶层浮层** — 与现有
  `#queuepop`/`#syspop` 相同的定位、外点关闭、Esc 关闭模式；桌面和手机一致。
- B：输入框上方常驻模板 chip 条 — 一眼可见，但模板多时占对话区，管理入口
  仍要另设。
- C：独立模板管理页 — 管理集中，但日常插入多一次跳转，改动也最大。

## 设计

### 存储与校验

新模块 `claude_sessions/prompts.py`：

- `PromptTemplates(path)`：线程安全，构造时加载，写操作持锁并原子落盘
  （复用 `session_preferences._atomic_json_dump` 的做法：同目录临时文件 +
  fsync + `os.replace`）。
- 路径：`CLAUDE_DIR/claude-sessions/prompt-templates.json`，与
  `approvals.json`、`efforts.json` 同级。
- 格式：

  ```json
  {
    "version": 1,
    "templates": [
      {"id": "a1b2c3d4e5f60718", "title": "修 bug",
       "text": "……", "created_at": 1763500000, "updated_at": 1763500000}
    ]
  }
  ```

- 校验：
  - 整个文件 `version == 1` 且 `templates` 是列表；任一记录不合法则整体
    不加载，`error` 置为「模板文件无法读取或格式损坏，修复或移走该文件后会自动重新加载」。
  - 新建/更新：`text` 去首尾空白后非空、≤ 20000 字符；`title` 去首尾空白、
    ≤ 80 字符；`title` 为空时取 `text` 第一行（按 Unicode 码点截到 40 字符）作为标题。
  - 文本和标题拒绝孤立 Unicode 代理项；`id` 限 1-64 位 `[A-Za-z0-9_-]`；
    `created_at`/`updated_at` 必须是有限数值。
  - 最多 100 条；新建插入列表头部，更新保持原位置。
  - `id` 用 `secrets.token_hex(8)`；`created_at` 保留，`updated_at` 刷新。
  - 每次读/写前比对文件 mtime 与 size：运行期间外部修改会自动重载；文件被删除
    视为空列表；损坏文件修复后无需重启即可恢复。写操作仍持锁原子落盘，成功后才
    发布内存状态。
- `Monitor.__init__` 创建 `self.prompts`，与 `approvals`/`efforts` 并列。

### HTTP API（`http_api.py`）

- `GET /prompt-templates` → `{"templates": [...]}`；文件损坏时返回 500
  `{"error": "..."}`，不影响其他接口。
- `POST /prompt-templates` body `{"id"?: string, "title": string, "text": string}`
  - 无 `id`：新建，返回 `200 {"template": {...}}`；
  - 有 `id`：更新，找不到返回 404；
  - 校验失败 400，保存失败 500。
- `DELETE /prompt-templates/<id>` → `{"ok": true}`；找不到 404。
- POST 复用现有 `_safe_json_write`（同源 + JSON Content-Type）；DELETE 复用
  `_safe_write_origin`。接口无鉴权，沿用「只暴露在可信网络」的前提。

### 前端（`templates/index.html`）

- `composerHtml` 在「图片」按钮旁加 `<button id="tplbtn">模板</button>`，
  会话页和新建会话页自动都有。
- body 顶层新增 `<div id="tplpop" hidden></div>`；样式复用 `#queuepop`：
  `position:fixed`、`z-index:200`、按按钮矩形定位并夹紧视口，滚动/resize
  重新定位。
- 打开时拉取 `GET /prompt-templates` 并缓存本次结果；关闭时清空并重置搜索。
- 浮层内容：

  ```text
  ┌──────────────────────────────┐
  │ 提示词模板            + 新建 │
  │ [搜索标题或内容…]            │
  │ 修 bug                      │
  │   按仓库风格修复……           │
  │   [插入] [编辑] [删除]        │
  │ 写测试                      │
  │   ……                        │
  │ [保存当前输入为模板]          │
  └──────────────────────────────┘
  ```

- 交互：
  - 点击模板标题/正文区域或「插入」：把文本插入到 `#msgbox` 光标处，有选区
    则替换选区；随后 `focus()`、触发输入处理（草稿版本 + autosize）；关闭浮层。
  - 「新建」/「保存当前输入为模板」：打开内联表单（标题 + 正文）。输入框里
    已有文字时正文预填，标题按首行自动生成；空输入时表单为空。
  - 「编辑」：同一表单带原 `id` 预填；「删除」经 `window.confirm` 后调用
    DELETE，并从当前列表移除。
  - 搜索按标题和正文做不区分大小写的包含匹配；无结果时显示提示。
  - Esc：编辑中先退回列表，否则关闭浮层；点击浮层外或滚动/resize 按现有
    浮层规则处理。
  - 所有模板文本经 `esc()` 渲染，不执行 markdown/HTML。
- 状态：`tplPop = {open, loading, error, list, query, editing}`；错误内联显示，
  不阻塞发送框。

### 测试

- `tests/test_prompt_templates.py`：
  - 存储：新建/更新/删除、标题自动生成、长度与数量上限、损坏文件、
    原子写后重新加载一致。
  - HTTP：GET/POST/DELETE 的正常路径与 400/404；跨站 POST 被拒绝。
- 前端（`tests/test_frontend_controls.js`）：
  - `composerHtml("conv"/"new")` 都包含 `#tplbtn`，页面包含 `#tplpop`。
  - 抽出纯函数测试：`insertTemplateText(value, start, end, text)` 的插入与
    选区替换、`templateFilter(list, query)`、`templateTitleFromText(text)`。
- 全量：`PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v`
  与 `node --test tests/test_frontend_controls.js`。
- 手工：`uv run monitor.py --port 8765`，在会话页/新建页各验证一次插入、
  保存当前输入、编辑、删除、搜索与移动端宽度。

### 文档

- `CLAUDE.md`：架构模块列表补 `prompts.py`，数据流补 API 与前端入口。
- `README.md` / `README.zh-CN.md`：功能列表补一条「保存常用提示词，一键插入」。