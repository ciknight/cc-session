# 系统详情面板与电源操作 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 看板 header 右上角新增「系统」入口：浮层展示 CPU/内存利用率，可触发重启/睡眠（带确认）。

**Architecture:** 后端在 `monitor.py` 加 `GET /system`（psutil 采样，缺依赖时 503）与 `POST /system/power`（平台命令表 + `subprocess.Popen` 不等待）；前端复用 `#queuepop` 的 body 顶层固定定位浮层模式渲染数据，电源操作用 `window.confirm`（与归档/cc-switch 现有模式一致）。设计文档见 `docs/superpowers/specs/2026-10-27-system-panel-design.md`。

**Tech Stack:** Python 标准库 + psutil（新增依赖）；前端为现有自包含 SPA（无框架）。

**与设计文档的两处细化（Task 6 会同步修订 spec）：**
1. 确认方式用 `window.confirm`，不是 `#mask` 页内对话框——代码库现有危险操作（归档、cc-switch 覆盖）都用 `window.confirm`，保持一致更轻。
2. 电源命令「先 `Popen`（spawn 即返回，不等待命令完成；Windows 重启带 `/t 3` 缓冲、rundll32 挂起有毫秒级延迟）再写 HTTP 响应」——这样 spawn 失败仍能返回 500 与原因，比纯「先响应后执行」多一层可观测性，且实际挂起都发生在响应写出之后。

---

### Task 1: 添加 psutil 依赖

**Files:**
- Modify: `pyproject.toml`、`uv.lock`（由 uv 自动改）
- Modify: `CLAUDE.md:36`（依赖描述行）

- [ ] **Step 1: 添加依赖**

```bash
uv add psutil
```

- [ ] **Step 2: 更新 CLAUDE.md 依赖描述**

把「无测试框架、无 lint 配置；唯一依赖是 Jinja2，其余为标准库。」改为：

```
- 无测试框架、无 lint 配置；直接依赖 Jinja2 与 psutil（系统面板采样），其余为标准库。
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock CLAUDE.md
git commit -m "chore: 添加 psutil 依赖用于系统面板采样"
```

---

### Task 2: 后端 `GET /system`

**Files:**
- Modify: `monitor.py`（import 区、`scan_slash_commands` 之前加函数、`do_GET` 内 `/healthz` 分支后）
- Test: `tests/test_management_api.py`（`ManagementAPITests` 类内追加）

- [ ] **Step 1: 写失败测试**

在 `tests/test_management_api.py` 的 `ManagementAPITests` 类内追加（`test_commands_endpoint_scans_user_and_project_dirs` 之后）：

```python
    def test_system_endpoint_returns_snapshot(self):
        fake = SimpleNamespace(
            cpu_percent=lambda interval=None, percpu=False: [11.0, 22.0] if percpu else 16.5,
            virtual_memory=lambda: SimpleNamespace(total=8 * 2**30, used=4 * 2**30,
                                                   available=4 * 2**30, percent=50.0),
            swap_memory=lambda: SimpleNamespace(total=2 * 2**30, used=2**30, percent=50.0),
            boot_time=lambda: time.time() - 3600,
        )
        with patch.object(monitor, "psutil", fake):
            result = json.load(self.request('/system'))
        self.assertEqual(result['cpu_percent'], 16.5)
        self.assertEqual(result['cpu_cores'], [11.0, 22.0])
        self.assertEqual(result['mem_total'], 8 * 2**30)
        self.assertEqual(result['mem_percent'], 50.0)
        self.assertEqual(result['swap_total'], 2 * 2**30)
        self.assertGreaterEqual(result['uptime'], 3599)
        self.assertTrue(result['hostname'])

    def test_system_endpoint_unavailable_without_psutil(self):
        with patch.object(monitor, "psutil", None):
            with self.assertRaises(HTTPError) as ctx:
                self.request('/system')
            self.assertEqual(ctx.exception.code, 503)
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api.ManagementAPITests.test_system_endpoint_returns_snapshot -v
```

预期：FAIL（AttributeError: module 'monitor' has no attribute 'psutil' 或 404 not found）。

- [ ] **Step 3: 实现**

3a. `monitor.py` import 区（`from cc_switch import ...` 之后）加可选导入：

```python
try:
    import psutil   # 系统面板采样用；未安装时 GET /system 返回 503
except ImportError:
    psutil = None
```

3b. `socket` 加入标准库 import（按字母序放在 `shutil` 与 `subprocess` 之间）。

3c. `scan_slash_commands` 函数之前加：

```python
class SystemInfoUnavailable(Exception):
    """系统状态采集不可用（psutil 缺失等）。"""


def system_snapshot():
    """采集一次系统状态。cpu_percent 阻塞约 0.3 秒采样，
    ThreadingHTTPServer 每请求独立线程，不影响其他请求。"""
    if psutil is None:
        raise SystemInfoUnavailable("psutil 未安装")
    cores = psutil.cpu_percent(interval=0.3, percpu=True)
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "hostname": socket.gethostname(),
        "uptime": max(0, int(time.time() - psutil.boot_time())),
        "cpu_percent": round(sum(cores) / len(cores), 1) if cores else 0.0,
        "cpu_cores": [round(c, 1) for c in cores],
        "mem_total": vm.total, "mem_used": vm.total - vm.available,
        "mem_available": vm.available, "mem_percent": vm.percent,
        "swap_total": swap.total, "swap_used": swap.used,
        "swap_percent": swap.percent,
    }
```

3d. `do_GET` 内 `/healthz` 分支之后加：

```python
            if u.path == "/system":
                try:
                    return self._json(system_snapshot())
                except SystemInfoUnavailable as e:
                    return self._json({"error": f"系统信息不可用：{e}"}, 503)
```

- [ ] **Step 4: 运行确认通过**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api -v
```

预期：全 PASS（含既有用例）。

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_management_api.py
git commit -m "feat: GET /system 返回 CPU/内存利用率快照"
```

---

### Task 3: 后端 `POST /system/power`

**Files:**
- Modify: `monitor.py`（`system_snapshot` 之后加函数、`do_POST` 内 `/hosted` 分支之前）
- Test: `tests/test_management_api.py`（`ManagementAPITests` 类内追加）

- [ ] **Step 1: 写失败测试**

`ManagementAPITests` 类内追加：

```python
    def test_power_command_platform_matrix(self):
        with patch.object(monitor.sys, "platform", "win32"):
            self.assertEqual(monitor.power_command("restart"), ["shutdown", "/r", "/t", "3"])
            self.assertEqual(monitor.power_command("sleep"),
                             ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        with patch.object(monitor.sys, "platform", "linux"):
            self.assertEqual(monitor.power_command("restart"), ["systemctl", "reboot"])
            self.assertEqual(monitor.power_command("sleep"), ["systemctl", "suspend"])
        with self.assertRaises(ValueError):
            monitor.power_command("halt")

    def test_power_endpoint_spawns_command(self):
        calls = []
        commands = {"restart": ["x", "r"], "sleep": ["x", "s"]}
        with patch.object(monitor, "power_command", lambda action: commands[action]), \
             patch.object(monitor.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd)):
            result = json.load(self.request('/system/power', {"action": "restart"},
                                            {"Content-Type": "application/json"}))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [["x", "r"]])

    def test_power_endpoint_rejects_unknown_action(self):
        with self.assertRaises(HTTPError) as ctx:
            self.request('/system/power', {"action": "shutdown"}, {"Content-Type": "application/json"})
        self.assertEqual(ctx.exception.code, 400)

    def test_power_endpoint_reports_spawn_failure(self):
        commands = {"restart": ["x", "r"]}
        def boom(cmd, **kw):
            raise OSError("no such file")
        with patch.object(monitor, "power_command", lambda action: commands[action]), \
             patch.object(monitor.subprocess, "Popen", boom):
            with self.assertRaises(HTTPError) as ctx:
                self.request('/system/power', {"action": "restart"},
                             {"Content-Type": "application/json"})
            self.assertEqual(ctx.exception.code, 500)
```

- [ ] **Step 2: 运行确认失败**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api.ManagementAPITests.test_power_endpoint_spawns_command -v
```

预期：FAIL（AttributeError: no attribute 'power_command'）。

- [ ] **Step 3: 实现**

3a. `system_snapshot` 之后加：

```python
def power_command(action):
    """返回平台对应的电源命令参数列表（由调用方 Popen，不等待完成）；
    动作非法时抛 ValueError。Windows 重启带 /t 3 缓冲，让前端先收到响应。"""
    if action == "restart":
        if sys.platform == "win32":
            return ["shutdown", "/r", "/t", "3"]
        if sys.platform == "darwin":
            return ["osascript", "-e", 'tell app "System Events" to restart']
        return ["systemctl", "reboot"]
    if action == "sleep":
        if sys.platform == "win32":
            return ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        if sys.platform == "darwin":
            return ["osascript", "-e", 'tell app "System Events" to sleep']
        return ["systemctl", "suspend"]
    raise ValueError("action 必须是 restart 或 sleep")
```

3b. `do_POST` 内 `_safe_json_write()` 检查之后、`/cc-switch/local` 分支之前加：

```python
            if u.path == "/system/power":
                body = self._read_body()
                action = body.get("action") if isinstance(body, dict) else None
                try:
                    cmd = power_command(action)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                try:
                    # spawn 即返回不等待命令完成；实际重启/挂起发生在响应写出之后
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                except OSError as e:
                    return self._json({"error": f"电源命令执行失败：{e}"}, 500)
                return self._json({"ok": True})
```

- [ ] **Step 4: 运行确认通过**

```bash
PYTHONPATH=. uv run python -m unittest tests.test_management_api -v
```

预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_management_api.py
git commit -m "feat: POST /system/power 重启/睡眠电源操作"
```

---

### Task 4: 前端系统按钮与浮层

**Files:**
- Modify: `templates/index.html`（CSS、header、`#syspop` 容器、JS）

无 node 测试（spec 已定），手测在 Task 6 统一做。

- [ ] **Step 1: CSS**（`#queuepop` 规则块之后追加）

```css
/* 系统面板浮层：同 #queuepop 挂 body 顶层固定定位，锚定 header 的系统按钮 */
#syspop{position:fixed;z-index:200;width:min(90vw,340px);background:var(--bg);border:1px solid var(--bd);
  border-radius:8px;padding:10px 14px;box-shadow:0 8px 24px rgba(0,0,0,.16);text-align:left}
#syspop[hidden]{display:none}
.sysrow{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:12px;color:var(--mut);margin-top:8px}
.sysbar{height:6px;background:var(--card2);border-radius:3px;overflow:hidden;margin-top:3px}
.sysbar>i{display:block;height:100%;background:var(--acc)}
.sysbar.warn>i{background:#d97706}
.sysbar.hot>i{background:#dc2626}
.syscores{display:grid;grid-template-columns:repeat(auto-fill,minmax(60px,1fr));gap:4px 10px;margin-top:6px}
.core{font-size:10px;color:var(--mut)}
.core .sysbar{height:4px;margin-top:2px}
.sysacts{display:flex;gap:8px;margin-top:12px;padding-top:10px;border-top:1px solid var(--bd)}
```

- [ ] **Step 2: header 与容器**（`<span id="err"></span>` 之后加按钮；`<div id="queuepop" hidden></div>` 之后加容器）

```html
<button id="sysbtn" class="back" style="margin-left:auto" title="系统状态与电源操作">🖥 系统</button>
```

```html
<div id="syspop" hidden></div>
```

- [ ] **Step 3: JS**（队列浮层 IIFE 块之后追加）

```js
/* ---------- 系统面板：header 右侧按钮 + body 顶层浮层（模式同 #queuepop） ---------- */
const sysPop = {timer: null, loading: false, note: ""};
let sysData = null;
function fmtGB(n){ return ((n || 0) / 2 ** 30).toFixed(1) + " GB"; }
function fmtUptime(sec){
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}
function barClass(p){ return p >= 85 ? "hot" : p >= 60 ? "warn" : ""; }
function sysPopHtml(data){
  if(!data) return '<div class="mut">加载中…</div>';
  if(data.error) return `<div class="terror">${esc(data.error)}</div>`;
  const cpu = data.cpu_percent || 0;
  const cores = (data.cpu_cores || []).map((p, i) =>
    `<div class="core"><span>核${i + 1} ${Math.round(p)}%</span><div class="sysbar ${barClass(p)}"><i style="width:${Math.min(100, p)}%"></i></div></div>`).join("");
  const mem = data.mem_percent || 0;
  return `<div class="sysrow"><span>CPU</span><b style="color:var(--fg);font-size:16px">${cpu.toFixed(1)}%</b></div>
    <div class="sysbar ${barClass(cpu)}"><i style="width:${Math.min(100, cpu)}%"></i></div>
    ${cores ? `<div class="syscores">${cores}</div>` : ""}
    <div class="sysrow"><span>内存</span><span>${fmtGB(data.mem_used)} / ${fmtGB(data.mem_total)}（${Math.round(mem)}%）</span></div>
    <div class="sysbar ${barClass(mem)}"><i style="width:${Math.min(100, mem)}%"></i></div>
    ${data.swap_total ? `<div class="sysrow"><span>交换</span><span>${fmtGB(data.swap_used)} / ${fmtGB(data.swap_total)}（${Math.round(data.swap_percent || 0)}%）</span></div>
    <div class="sysbar"><i style="width:${Math.min(100, data.swap_percent || 0)}%"></i></div>` : ""}
    <div class="sysrow" style="margin-top:10px"><span>已运行 ${fmtUptime(data.uptime || 0)} · ${esc(data.hostname || "")}</span></div>
    ${sysPop.note ? `<div role="status" style="margin-top:6px">${esc(sysPop.note)}</div>` : ""}
    <div class="sysacts"><button class="ibtn danger" data-power="restart">重启</button>
      <button class="ibtn danger" data-power="sleep">睡眠</button></div>`;
}
function positionSysPop(){
  const pop = document.getElementById("syspop"), btn = document.getElementById("sysbtn");
  if(!pop || pop.hidden || !btn) return;
  const rect = btn.getBoundingClientRect && btn.getBoundingClientRect();
  const root = document.documentElement;
  const vw = root ? root.clientWidth : 0, vh = root ? root.clientHeight : 0;
  if(!rect || !vw || !vh) return;   // 无布局环境（单测假 DOM）时只展示不定位
  const w = pop.offsetWidth || 0, h = pop.offsetHeight || 0;
  let left = Math.min(Math.max(8, rect.right - w), vw - w - 8);
  let top = rect.bottom + 6;   // 优先浮在按钮下方，放不下再向上
  if(top + h > vh - 8) top = Math.max(8, rect.top - h - 6);
  pop.style.left = Math.round(left) + "px";
  pop.style.top = Math.round(top) + "px";
}
function renderSysPop(){
  const pop = document.getElementById("syspop");
  if(!pop || !sysPop.timer) return;
  pop.innerHTML = sysPopHtml(sysData);
  pop.hidden = false;
  positionSysPop();
}
async function loadSystem(){
  if(sysPop.loading || !sysPop.timer) return;   // 上一请求未结束不叠加
  sysPop.loading = true;
  try{
    const r = await fetchT("/system");
    let j = null;
    try{ j = await r.json(); }catch(e){}
    sysData = r.ok ? j : {error: (j && j.error) || `HTTP ${r.status}`};
  }catch(e){ sysData = {error: "查询失败：" + (e && e.message || e)}; }
  sysPop.loading = false;
  renderSysPop();
}
function toggleSysPop(open){
  const pop = document.getElementById("syspop");
  if(!pop || open === (sysPop.timer != null)) return;
  if(open){
    sysData = null; sysPop.note = "";
    renderSysPop();
    loadSystem();
    sysPop.timer = setInterval(loadSystem, 3000);   // 展开期间 3 秒自动刷新
  }else{
    clearInterval(sysPop.timer); sysPop.timer = null;
    pop.hidden = true; pop.innerHTML = "";
  }
}
async function sendPower(action){
  const text = action === "restart"
    ? "确定要重启这台电脑？正在执行的会话任务会被中断。"
    : "确定要使这台电脑进入睡眠？";
  if(!window.confirm(text)) return;
  try{
    const r = await fetchT("/system/power", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({action})});
    let j = null;
    try{ j = await r.json(); }catch(e){}
    sysPop.note = r.ok && j && j.ok
      ? (action === "restart" ? "已发送重启指令，服务即将中断…" : "已发送睡眠指令…")
      : (j && j.error) || "执行失败";
  }catch(e){ sysPop.note = "执行失败：" + (e && e.message || e); }
  renderSysPop();
}
document.getElementById("sysbtn").addEventListener("click", e => {
  e.stopPropagation();
  toggleSysPop(sysPop.timer == null);
});
document.getElementById("syspop").addEventListener("click", e => {
  const btn = e.target.closest && e.target.closest("[data-power]");
  if(btn){ e.preventDefault(); sendPower(btn.dataset.power); }
});
document.addEventListener("click", e => {
  if(!sysPop.timer) return;
  const pop = document.getElementById("syspop");
  if(pop && pop.contains && pop.contains(e.target)) return;
  if(e.target.closest && e.target.closest("#sysbtn")) return;
  toggleSysPop(false);
});
document.addEventListener("keydown", e => { if(e.key === "Escape" && sysPop.timer) toggleSysPop(false); });
document.addEventListener("scroll", () => { if(sysPop.timer) positionSysPop(); }, true);
window.addEventListener("resize", () => { if(sysPop.timer) positionSysPop(); });
```

- [ ] **Step 4: 冒烟**

```bash
node --test tests/test_frontend_controls.js
```

预期：全 PASS（系统面板无新测试，确认没破坏既有用例）。

- [ ] **Step 5: Commit**

```bash
git add templates/index.html
git commit -m "feat: header 系统按钮与 CPU/内存浮层、重启/睡眠操作"
```

---

### Task 5: 手动验证（电源操作只验到确认框）

⚠️ **不要真的点确认重启**——验证 `GET /system`、浮层渲染、自动刷新、确认弹窗文案；电源 POST 用 curl 验证 400 分支即可。

- [ ] **Step 1: 启动服务**

```bash
uv run monitor.py --port 8799
```

- [ ] **Step 2: 浏览器验证**

打开 http://127.0.0.1:8799，逐项确认：
- header 最右出现「🖥 系统」按钮；
- 点击浮层出现并锚定按钮下方，含 CPU 总量+每核条、内存/交换条、运行时长；
- 等 3 秒数值自动刷新；点页面空白处或按 Esc 收起；
- 点「重启」弹 confirm 文案含「重启这台电脑」，点取消无副作用；「睡眠」同理。

- [ ] **Step 3: curl 验证错误分支**

```bash
curl -s -X POST http://127.0.0.1:8799/system/power -H "Content-Type: application/json" -d '{"action":"shutdown"}'
```

预期：`{"error":"action 必须是 restart 或 sleep"}`（HTTP 400）。

- [ ] **Step 4: 关闭服务**

---

### Task 6: 文档同步与 spec 修订

**Files:**
- Modify: `monitor.py`（模块 docstring 接口列表）
- Modify: `CLAUDE.md`（HTTP 层与前端章节）
- Modify: `docs/superpowers/specs/2026-10-27-system-panel-design.md`

- [ ] **Step 1: monitor.py docstring**（`GET /healthz` 行之后加）

```
  GET /system                宿主机 CPU/内存利用率快照（psutil 采样，缺依赖时 503）
  POST /system/power         电源操作 {"action": "restart|sleep"}，仅前端 confirm 防误触
```

- [ ] **Step 2: CLAUDE.md**

- 「架构」章节数据流第 3 条（HTTP 层）末尾补一句：

```
`GET /system` 返回宿主机 CPU/内存快照（psutil），`POST /system/power` 执行重启/睡眠
（Popen 不等待、Windows 重启带 /t 3 缓冲）。
```

- 「前端」章节末尾补一句：

```
header 右侧「系统」按钮弹出 `#syspop` 浮层（模式同 `#queuepop`）：CPU/内存利用率
条形图、展开期间 3 秒自动刷新，底部重启/睡眠经 `window.confirm` 二次确认后 POST。
```

- [ ] **Step 3: 修订 spec 与实现的差异**（`docs/superpowers/specs/2026-10-27-system-panel-design.md`）

- 「复用 `#mask` 遮罩 + 现有对话框模式」→ 「复用 `window.confirm`（与归档/cc-switch 现有模式一致）」；
- 「**先发 HTTP 响应 `{"ok": true}`，再触发命令**」→ 「Popen 先 spawn（不等待命令完成；Windows 重启 `/t 3`、rundll32 挂起有毫秒级延迟，实际生效在响应写出之后）再写响应，spawn 失败返回 500」。

- [ ] **Step 4: 全量回归**

```bash
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/test_frontend_controls.js
```

预期：全 PASS。

- [ ] **Step 5: Commit**

```bash
git add monitor.py CLAUDE.md docs/superpowers/specs/2026-10-27-system-panel-design.md
git commit -m "docs: 系统面板接口与前端说明，同步 spec 确认方式"
```
