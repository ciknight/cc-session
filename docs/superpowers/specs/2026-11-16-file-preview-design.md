# 文件在线预览 — 设计

日期：2026-11-16
状态：已实施（见 docs/superpowers/plans/2026-11-16-file-preview.md）

## 意图

看板对话消息里 AI 会写相对仓库根目录的 markdown 链接（如
`[spec](docs/superpowers/specs/xxx.md)`）。浏览器按 URL 结构把相对 href
解析到当前 SPA 路由 `/p/<项目>/s/<会话ID>` 上，得到
`/p/F:/code/…/s/docs/superpowers/specs/xxx.md` 这种不存在的「项目路径」，
点开后页面为空。希望点击即可在新标签页预览磁盘上的真实文件。

**关键约束：AI 不能也不会配合**——它不知道看板存在，只会写普通 markdown
相对链接 / 绝对路径。所有适配必须发生在看板侧。

## 目标 / 非目标

- 目标：消息中 AI 生成的文件链接点击后在新标签预览；md 渲染、文本/代码
  纯文本展示、图片直显；直接访问已被错误解析的 `/p/...` URL 也能兜底跳预览。
- 非目标：不做文件树浏览、不做编辑、不做语法高亮库引入。
- 反目标：要求 AI 生成任何特殊链接格式。

## 方案对比

- **A（选定）：独立轻量预览页** — `GET /view?path=<绝对路径>` 渲染独立
  `preview.html`（仅引本地 static 的 marked/purify，无 SSE/轮询），内容经
  `GET /file?path=` 读取。页面轻，不拖累主 SPA。
- B：做进主 SPA 一个视图 — 复用样式但每次预览加载整个看板（SSE、轮询全起），重。
- C：`/file` 交给浏览器原生显示 — 最省，但 md 只见纯文本，达不到预览目的。

## 设计

### 后端（http_api.py）

- `GET /view?path=<abs>`：渲染 `templates/preview.html`。
- `GET /file?path=<abs>`：按扩展名分类：
  - md/常见代码与文本 → `text/plain; charset=utf-8`（utf-8 严格解码失败回退
    gb18030，再失败 replace）；文本上限 2 MiB，超出截断并附提示行。
  - png/jpg/jpeg/gif/webp/bmp → 原始字节 + 对应 content-type（上限 32 MiB）。
  - svg 一律按文本（避免同源脚本执行）。
  - 其余二进制 → `application/octet-stream`，由预览页提示不支持并给下载链接。
- 参数校验：路径须绝对（Windows 盘符/UNC 均可，`os.path.abspath` 归一），
  须存在且是文件；否则 400/404。
- 不限制目录范围：服务本就无鉴权、仅本机信任（已有接口可重启宿主机），
  沿用「不暴露到不可信网络」前提。

### 前端（index.html）

- **入口 1（点击，主路径）**：会话视图消息组新建 DOM 节点时，重写其中的
  `<a href>`：
  - 无 scheme（不含 `xxx:`）且非 `//` 开头 → 相对**会话 cwd**（`convState.detail.cwd`）
    解析为绝对路径；
  - `F:/…`、`F:\…` 绝对路径或 `file:///…` → 提取本地路径；
  - 重写为 `/view?path=<encodeURIComponent(abs)>`，`target="_blank"`
    （中键/Ctrl 点/复制链接同样有效）；
  - http(s)、`#锚点`、mailto 等不动。
- **入口 2（直接访问兜底）**：`resolveViewProject` 中项目路径匹配不到已知项目、
  但形如 `<已知项目>/s/<相对文件路径>` 时重定向到 `/view`；会话段本身形如
  文件名（结尾带 `.ext`，会话 ID 均为 UUID / `parent::agent-hash`，永不带点号）
  时同步判定直接跳预览、不发探测请求。正好覆盖已流传的错误 URL。
  已知取舍：无扩展名文件（`LICENSE`、`Makefile`）的根目录链接仍走原
  「会话不存在」路径，不做探测。
- 预览页内部：md 的相对链接相对**被预览文件所在目录**解析，继续跳 `/view`
  （spec 互链可用）；顶部显示完整路径、大小、类型与「原始内容」链接。

### 测试

- unittest：`/file`（各类型、截断、编码回退、404/400、目录）、`/view` 模板。
- 前端 node 测试：抽出纯函数（href 分类与重写判定、相对路径解析、扩展名分类）。
