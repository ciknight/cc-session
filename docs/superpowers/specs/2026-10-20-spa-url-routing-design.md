# SPA URL 路由设计

日期：2026-10-20
状态：已确认

## 目标

看板当前所有视图都停留在 `http://127.0.0.1:8765/`，浏览器前进/后退无法在项目列表、
会话对话和新建会话之间切换。为每个视图分配真实 URL path，使用 History API 同步，
使刷新、直接访问链接、前进后退都能还原对应视图。

## 路由结构

| URL | 视图 |
|---|---|
| `/` | 首页；数据加载后自动选中最近活跃项目，并用 `replaceState` 同步为 `/p/<项目>` |
| `/p/<project>` | 该项目会话列表 |
| `/p/<project>/s/<sessionId>` | 会话对话页 |
| `/p/<project>/new` | 新建会话页 |

`<project>` 是项目根路径（`project_root` 的结果，与 `view.project` 一致），
`<sessionId>` 是会话 id。项目路径按目录层级展开为可读形式：反斜杠归一为 `/`，
盘符冒号保留，只对空格、`#`、`?`、中文等会破坏 URL 的字符做百分号编码。
Windows 示例：`/p/F:/code/claude-sessions/s/0f3c...`。
旧格式（整条路径编码为 `%3A%5C...` 单个路径段）仍能解析，并会被规整为可读路径。

## 前端（`templates/index.html`）

新增纯函数，便于在 Node vm 测试中直接调用：

- `encSeg` / `projectUrlPath`：逐段编码并把项目路径展开成可读的 `/` 分隔形式。
- `routePath(view)`：由 `{project, session, new}` 生成 URL；无项目时返回 `/`。
- `parseRoute(pathname)`：解析 URL；非 `/p/...` 结构、空项目、非法百分号编码返回 `null`。
  返回的 `fullProject` 保留未剥离的完整路径，供歧义消解使用。
- `resolveViewProject(projects)`：把 URL 中的可读路径映射回系统原生 cwd；
  若完整路径本身就是一个项目（目录名恰好是 `new` 或形如 `.../s/xxx`），优先按项目列表解析。
- `readRoute()`：读取 `window.location.pathname` 并调用 `parseRoute`，无 `window.location`
  时返回 `null`（兼容测试环境）。
- `syncUrl(view, replace)`：URL 与目标不一致时调用 `pushState`/`replaceState`；
  `window.history` 不可用时静默跳过。
- `navigate(next, {replace})`：设置 `view`；会话变化时断开旧会话 SSE、清空
  `convCache`/`convState`/`interCache` 与流式气泡；同步 URL；调用 `renderMain()`。

导航入口统一改走 `navigate`：侧栏项目、项目新建按钮、列表页“＋ 新建会话”、
会话卡片、对话页“← 返回列表”、新建页“← 返回列表”、待审批徽章。
新建会话拿到 session id 后改为 `replaceState` 到对话 URL，避免返回时回到一次性的新建页。

启动时若 URL 可解析，先把 `view` 设为该路由，再由首次 `load()` 渲染，避免在
`/sessions` 数据到达前把项目误判为不存在而覆盖。监听 `popstate`：重新解析 URL、
恢复 `view`、清理上个会话状态并渲染。

`renderMain()` 解析出默认项目后调用 `syncUrl(view, true)`：URL 已是目标时无操作，
因此轮询/SSE 刷新不会新增历史记录。

### 边界行为

- URL 中项目已不存在：回退到最近活跃项目，并用 `replaceState` 修正 URL。
- URL 中会话尚未被轮询发现：沿用现有“会话转录尚未就绪，稍候自动刷新…”占位。
- 非法路由（如 `/p/`、无项目、错误编码）：`parseRoute` 返回 `null`，按首页处理。

## 服务端（`monitor.py`）

`GET /` 与 `GET /p/...` 返回 SPA 模板；其余 API 路由（`/sessions`、`/static`、
`/events` 等）保持不变。未知的 `/p/...` 子路径也返回模板，由前端裁决视图。

## 测试

- 前端（`tests/test_frontend_controls.js`）：`routePath` 生成、`parseRoute` 解析
  （项目列表/对话/新建/非法编码/根路径/旧编码链接）、以及 `resolveViewProject` 的歧义消解。
- 服务端（`tests/test_management_api.py`）：`GET /p/demo` 返回 `text/html` 且含
  SPA 标记。

## 非目标

- 不引入第三方路由库。
- 不改变 API 路径、SSE 行为或轮询/缓存逻辑。
- 不支持浏览器标签页分离状态或多窗口同步。