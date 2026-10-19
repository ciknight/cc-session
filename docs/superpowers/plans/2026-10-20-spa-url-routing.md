# SPA URL 路由实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给看板加真实 URL path，使项目列表、会话对话、新建会话各有地址，浏览器前进/后退和刷新可直接还原视图。

**Architecture:** 前端用 History API 维护 `{project, session, new}` 与 URL 的双向映射，导航统一经过 `navigate()`；服务端把 `GET /p/...` 和 `GET /` 一样渲染同一个 SPA 模板，其余 API 路由不变。

**Tech Stack:** 原生浏览器 History API、内联 JS（`templates/index.html`）、Python 标准库 HTTP 服务（`monitor.py`）、Node `node:test`、Python `unittest`。

**Spec:** `docs/superpowers/specs/2026-10-20-spa-url-routing-design.md`

---

### Task 1: 前端路由纯函数

**Files:**
- Modify: `templates/index.html`（在 `function md(text){` 之前插入）
- Test: `tests/test_frontend_controls.js`（文件末尾追加）

- [ ] **Step 1: 写失败测试**

在 `tests/test_frontend_controls.js` 末尾追加：

```js
test('URL 路由可生成并解析项目/会话/新建视图', () => {
  const b = boot();
  const cwd = 'F:\\code\\proj one';
  b.context.v = {project: cwd, session: null, new: false};
  assert.equal(b.run('routePath(v)'), '/p/F%3A%5Ccode%5Cproj%20one');
  b.context.v = {project: cwd, session: '0f3c-uuid', new: false};
  assert.equal(b.run('routePath(v)'), '/p/F%3A%5Ccode%5Cproj%20one/s/0f3c-uuid');
  b.context.v = {project: cwd, session: null, new: true};
  assert.equal(b.run('routePath(v)'), '/p/F%3A%5Ccode%5Cproj%20one/new');
  b.context.v = {project: null, session: null, new: false};
  assert.equal(b.run('routePath(v)'), '/');

  b.context.path = '/p/F%3A%5Ccode%5Cproj%20one/s/abc';
  assert.deepEqual({...b.run('parseRoute(path)')}, {project: cwd, session: 'abc', new: false});
  b.context.path = '/p/F%3A%5Ccode%5Cproj%20one';
  assert.deepEqual({...b.run('parseRoute(path)')}, {project: cwd, session: null, new: false});
  b.context.path = '/p/F%3A%5Ccode%5Cproj%20one/new';
  assert.deepEqual({...b.run('parseRoute(path)')}, {project: cwd, session: null, new: true});

  for(const bad of ['/', '/p/', '/p/x/unknown', '/p/x/s/', '/s/abc', '/p/%E0%A4%A']){
    b.context.path = bad;
    assert.equal(b.run('parseRoute(path)'), null, bad);
  }
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test tests/test_frontend_controls.js`

Expected: FAIL，`routePath is not defined`（或同类 ReferenceError）。

- [ ] **Step 3: 实现纯函数**

在 `templates/index.html` 中，把：

```js
function md(text){
```

替换为：

```js
/* ---------- URL 路由 ---------- */
// 项目根路径与会话 id 都作为单个路径段，编码后直接拼进 URL
function routePath(v){
  if(!v || !v.project) return "/";
  const base = "/p/" + encodeURIComponent(v.project);
  if(v.session) return base + "/s/" + encodeURIComponent(v.session);
  if(v.new) return base + "/new";
  return base;
}
function parseRoute(pathname){
  if(!pathname) return null;
  const parts = pathname.split("/").filter(Boolean);
  if(parts.length < 2 || parts.length > 4 || parts[0] !== "p") return null;
  let project;
  try{ project = decodeURIComponent(parts[1]); }catch{ return null; }
  if(!project) return null;
  if(parts.length === 2) return {project, session: null, new: false};
  if(parts.length === 3 && parts[2] === "new") return {project, session: null, new: true};
  if(parts.length === 4 && parts[2] === "s"){
    let session;
    try{ session = decodeURIComponent(parts[3]); }catch{ return null; }
    return session ? {project, session, new: false} : null;
  }
  return null;
}

function md(text){
```

- [ ] **Step 4: 运行测试确认通过**

Run: `node --test tests/test_frontend_controls.js`

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 前端 URL 路由解析与生成"
```

---

### Task 2: 服务端 SPA 路由回退

**Files:**
- Modify: `monitor.py`（`_raw` 之后新增 `_index`；`do_GET` 的 `/` 分支）
- Test: `tests/test_management_api.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_management_api.py` 的 `ManagementAPITests` 类中追加：

```python
    def test_spa_route_serves_index_html(self):
        for path in ("/", "/p/demo%5Cproject"):
            response = self.request(path)
            self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertIn("Claude Sessions", response.read().decode("utf-8"))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api.ManagementAPITests.test_spa_route_serves_index_html -v`

Expected: FAIL，`/p/demo%5Cproject` 返回 `HTTPError: 404`。

- [ ] **Step 3: 抽出模板渲染并加回退**

在 `monitor.py` 的 `_raw` 方法之后新增：

```python
        def _index(self):
            body = _jinja.get_template("index.html").render(
                refresh_interval=refresh_interval,
                stale_after=mon.stale_after,
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
```

把 `do_GET` 中的：

```python
            if u.path == "/":
                body = _jinja.get_template("index.html").render(
                    refresh_interval=refresh_interval,
                    stale_after=mon.stale_after,
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
```

替换为：

```python
            if u.path == "/" or u.path.startswith("/p/"):
                # /p/<项目>[/s/<会话>|/new] 都由前端路由裁决，服务端统一回模板
                return self._index()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONPATH=. uv run python -m unittest tests.test_management_api.ManagementAPITests.test_spa_route_serves_index_html -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add monitor.py tests/test_management_api.py
git commit -m "feat: 服务端支持 SPA 子路径回退"
```

---

### Task 3: 导航接入 History API

**Files:**
- Modify: `templates/index.html`
- Test: `tests/test_frontend_controls.js`（扩充 boot 测试环境并追加用例）

- [ ] **Step 1: 扩充测试环境并写失败测试**

把 `tests/test_frontend_controls.js` 的 `boot()` 中：

```js
  const documentListeners = {};
  const context = vm.createContext({document:{
      getElementById:node,
      createElement:tag=>({tagName:tag.toUpperCase(), value:'', textContent:'', dataset:{}}),
      addEventListener(type, listener){(documentListeners[type] ||= []).push(listener);},
    }, window:{confirm:()=>true},
    localStorage:{getItem(){return null;}}, fetch, AbortController, setTimeout, clearTimeout,
    setInterval(){}, clearInterval(){}, console});
  vm.runInContext(source, context);
  return {node, run: s => vm.runInContext(s, context), context, documentListeners};
```

替换为：

```js
  const documentListeners = {};
  const historyCalls = [];
  const location = {pathname: "/"};
  const history = {
    pushState(state, title, url){ historyCalls.push(["push", url]); location.pathname = url; },
    replaceState(state, title, url){ historyCalls.push(["replace", url]); location.pathname = url; },
  };
  const context = vm.createContext({document:{
      getElementById:node,
      createElement:tag=>({tagName:tag.toUpperCase(), value:'', textContent:'', dataset:{}}),
      addEventListener(type, listener){(documentListeners[type] ||= []).push(listener);},
    }, window:{confirm:()=>true, location, history},
    localStorage:{getItem(){return null;}}, fetch, AbortController, setTimeout, clearTimeout,
    setInterval(){}, clearInterval(){}, console});
  vm.runInContext(source, context);
  return {node, run: s => vm.runInContext(s, context), context, documentListeners,
          historyCalls, location};
```

在文件末尾追加：

```js
test('导航同步 URL，并在默认项目解析后替换首页地址', () => {
  const b = boot();
  b.context.__s = {id:'s1', project:'F:\\code\\p', cwd:'F:\\code\\p',
                   state:'idle', tokens:{}};
  b.run('sessions = [__s]');

  b.context.__next = {project:'F:\\code\\p', session:null, new:false};
  b.run('navigate(__next)');
  assert.deepEqual(b.historyCalls, [['push', '/p/F%3A%5Ccode%5Cp']]);
  assert.equal(b.location.pathname, '/p/F%3A%5Ccode%5Cp');

  b.context.__next = {project:'F:\\code\\p', session:'s1', new:false};
  b.run('navigate(__next)');
  assert.deepEqual(b.historyCalls[1], ['push', '/p/F%3A%5Ccode%5Cp/s/s1']);
  assert.equal(b.run('view.session'), 's1');

  b.context.__next = {project:'F:\\code\\p', session:null, new:false};
  b.run('navigate(__next)');
  assert.equal(b.historyCalls.length, 3);

  // 回到根路径：数据已加载，应自动选中项目并 replace 成项目地址
  b.location.pathname = '/';
  b.run('navigate({project:null, session:null, new:false})');
  assert.deepEqual(b.historyCalls[3], ['replace', '/p/F%3A%5Ccode%5Cp']);
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node --test tests/test_frontend_controls.js`

Expected: FAIL，`navigate is not defined`。

- [ ] **Step 3: 添加导航函数**

在 `templates/index.html` 的 `/* ---------- 主渲染 ---------- */` 之前插入：

```js
/* ---------- 导航与浏览器历史 ---------- */
function historyApi(){
  return typeof window !== "undefined" && window.history
    && typeof window.history.pushState === "function" ? window.history : null;
}
function readRoute(){
  const loc = typeof window !== "undefined" ? window.location : null;
  return loc ? parseRoute(loc.pathname) : null;
}
function syncUrl(v, replace){
  const h = historyApi(), loc = h ? window.location : null;
  if(!h || !loc) return;
  const next = routePath(v);
  if(next === loc.pathname) return;
  try{ (replace ? h.replaceState : h.pushState).call(h, null, "", next); }catch(e){ }
}
function clearSessionView(){
  disconnectSessionSse();
  clearLiveBubble();
  convCache = {id: null, size: -1};
  convState = {id: null, msgs: [], total: 0, cursor: 0, start: 0, size: -1,
               hasMore: false, loadingEarlier: false};
  interCache = {id: null, sig: ""};
}
function navigate(next, opts){
  next = next || {project: null, session: null, new: false};
  const leaving = view.session !== (next.session || null);
  view = next;
  if(leaving) clearSessionView();
  syncUrl(view, !!(opts && opts.replace));
  renderMain();
}
function onPopState(){
  navigate(readRoute() || {project: null, session: null, new: false});
}
```

- [ ] **Step 4: 接入所有导航入口**

**4a.** 把 `renderMain()` 中的：

```js
  if(!view.project || !projects.some(p => p.cwd === view.project))
    // 侧栏固定排序后，初次进入仍默认选中最近活跃的项目
    view.project = projects.length
      ? projects.reduce((a, b) => b.last > a.last ? b : a).cwd : null;
  renderSidebar(projects);   // 更新 active 高亮
```

替换为：

```js
  if(!view.project || !projects.some(p => p.cwd === view.project))
    // 侧栏固定排序后，初次进入仍默认选中最近活跃的项目
    view.project = projects.length
      ? projects.reduce((a, b) => b.last > a.last ? b : a).cwd : null;
  if(view.project) syncUrl(view, true);   // 默认项目解析后用 replace 落地址，不新增历史记录
  renderSidebar(projects);   // 更新 active 高亮
```

**4b.** 把对话页返回按钮：

```js
        document.getElementById("back").onclick = () => {
          disconnectSessionSse();
          view.session = null;
          convCache = {id: null, size: -1};
          convState = {id: null, msgs: [], total: 0, cursor: 0, start: 0, size: -1,
                       hasMore: false, loadingEarlier: false};
          renderMain();
        };
```

替换为：

```js
        document.getElementById("back").onclick = () => {
          navigate({project: view.project, session: null, new: false});
        };
```

**4c.** 把新建页返回按钮：

```js
  document.getElementById("back").onclick = () => { view.new = false; renderMain(); };
```

替换为：

```js
  document.getElementById("back").onclick = () => {
    navigate({project: view.project, session: null, new: false});
  };
```

**4d.** 把 `enterSession` 中的：

```js
      if(view.new && view.project === p.cwd){
        view = {project: p.cwd, session: sid, new: false};
        convCache = {id: null, size: -1};
        renderConversation(sid, true);   // 转录尚未被轮询发现时先显示占位，稍后自动刷新补上
      }
```

替换为：

```js
      if(view.new && view.project === p.cwd){
        // 新建页是一次性入口，用 replace 换成会话地址，返回时直接回到项目列表
        navigate({project: p.cwd, session: sid, new: false}, {replace: true});
      }
```

**4e.** 把待审批徽章：

```js
document.getElementById("pendingbadge").onclick = () => {
  const target = sessions.find(s => s.pending_interactions > 0);
  if(!target) return;
  view = {project: target.project, session: target.id, new: false};
  convCache = {id: null, size: -1};
  renderConversation(target.id, true);
};
```

替换为：

```js
document.getElementById("pendingbadge").onclick = () => {
  const target = sessions.find(s => s.pending_interactions > 0);
  if(!target) return;
  navigate({project: target.project, session: target.id, new: false});
};
```

**4f.** 把侧栏点击处理：

```js
  if(nb){
    view = {project: nb.dataset.cwd, session: null, new: true};
    convCache = {id: null, size: -1};
    toggleDrawer(false);
    renderMain();
    return;
  }
  const item = e.target.closest(".pitem");
  if(!item) return;
  view = {project: item.dataset.cwd, session: null};
  convCache = {id: null, size: -1};
  toggleDrawer(false);
  renderMain();
```

替换为：

```js
  if(nb){
    toggleDrawer(false);
    navigate({project: nb.dataset.cwd, session: null, new: true});
    return;
  }
  const item = e.target.closest(".pitem");
  if(!item) return;
  toggleDrawer(false);
  navigate({project: item.dataset.cwd, session: null, new: false});
```

**4g.** 把主区点击处理：

```js
  if(e.target.closest("#newp")){
    view = {project: view.project, session: null, new: true};
    renderMain();
    return;
  }
  const card = e.target.closest(".scard");
  if(!card) return;
  view.session = card.dataset.id;
  view.new = false;
  convCache = {id: null, size: -1};
  renderConversation(view.session, true);
```

替换为：

```js
  if(e.target.closest("#newp")){
    navigate({project: view.project, session: null, new: true});
    return;
  }
  const card = e.target.closest(".scard");
  if(!card) return;
  navigate({project: view.project, session: card.dataset.id, new: false});
```

**4h.** 把启动代码：

```js
if(window.marked) marked.setOptions({breaks: true});
connectGlobalSse();
load();
resetTimer();
```

替换为：

```js
// 启动时先按地址栏恢复视图，再等首次 /sessions 数据渲染；
// 不在数据到达前 renderMain，避免空项目列表把 URL 里的项目覆盖掉
const bootRoute = readRoute();
if(bootRoute) view = bootRoute;
if(window.addEventListener) window.addEventListener("popstate", onPopState);
if(window.marked) marked.setOptions({breaks: true});
connectGlobalSse();
load();
resetTimer();
```

- [ ] **Step 5: 运行前端测试确认通过**

Run: `node --test tests/test_frontend_controls.js`

Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 看板视图与 URL 同步并支持前进后退"
```

---

### Task 4: 文档与全量验证

**Files:**
- Modify: `README.md`（接口表）
- Modify: `CLAUDE.md`（关键设计决策）

- [ ] **Step 1: 更新 README**

把：

```markdown
| `GET /` | 网页看板：左侧项目列表，右侧会话列表/对话视图，自动刷新可开关 |
```

替换为：

```markdown
| `GET /`、`GET /p/...` | 网页看板：左侧项目列表，右侧会话列表/对话视图，自动刷新可开关；`/p/<项目>`、`/p/<项目>/s/<会话>`、`/p/<项目>/new` 支持直接访问与浏览器前进后退 |
```

- [ ] **Step 2: 更新 CLAUDE.md**

在“前端实时更新”条目之后追加：

```markdown
- **URL 路由**：看板视图与地址栏同步，`/p/<encodeURIComponent(项目根路径)>` 是项目列表，
  `/p/<项目>/s/<会话ID>` 是对话，`/p/<项目>/new` 是新建；`GET /p/...` 与 `/` 一样回退渲染
  同一 SPA 模板。导航统一走 `navigate`（`pushState`），首页自动选中项目时用 `replaceState`
  避免多余历史记录；`popstate` 重新解析地址并恢复视图。
```

- [ ] **Step 3: 全量测试**

Run:

```bash
node --test tests/test_frontend_controls.js
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: 两套测试全部 PASS。

- [ ] **Step 4: 服务端冒烟**

Run:

```bash
uv run monitor.py --port 8766
```

另开终端：

```bash
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://127.0.0.1:8766/
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' 'http://127.0.0.1:8766/p/F%3A%5Ccode%5Cclaude-sessions'
```

Expected: 两行都是 `200 text/html; charset=utf-8`。浏览器打开首页，点击项目/会话后地址栏变化，
浏览器后退回到列表、前进回到会话；刷新子路径仍停留在同一视图。

- [ ] **Step 5: 提交**

```bash
git add README.md CLAUDE.md
git commit -m "docs: 补充看板 URL 路由说明"
```