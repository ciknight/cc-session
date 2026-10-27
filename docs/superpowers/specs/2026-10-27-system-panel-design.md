# 系统详情面板与电源操作设计

日期：2026-10-27

## 背景与目标

看板 header 右上角新增「系统」入口：点击弹出浮层，按需请求并展示宿主机
CPU/内存状态与利用率，浮层底部提供重启与睡眠两个电源操作按钮。
项目定位是局域网个人工具（接口无鉴权，见 CLAUDE.md），电源操作只做
前端二次确认弹窗，不引入令牌校验或倒计时取消，保持轻量。

## 后端（monitor.py）

### `GET /system`

返回系统状态 JSON：

```json
{
  "hostname": "…",
  "uptime": 123456,          // 秒
  "cpu_percent": 12.3,       // 总体利用率
  "cpu_cores": [10.0, 20.0], // 每核利用率，与逻辑核数等长
  "mem_total": 34359738368,
  "mem_used": 17179869184,
  "mem_available": 17179869184,
  "mem_percent": 50.0,
  "swap_total": 0,
  "swap_used": 0,
  "swap_percent": 0.0
}
```

- 采样用 `psutil.cpu_percent(interval=0.3, percpu=True)`：阻塞约 0.3 秒，
  `ThreadingHTTPServer` 每请求独立线程，不阻塞其他请求；面板按需请求、
  频率低（打开后 3 秒一次），无独立采样线程。
- `uptime` 由 `time.time() - psutil.boot_time()` 计算。
- psutil 导入失败（未安装）返回 503 `{"error": "系统信息不可用：<原因>"}`，
  前端在浮层内展示错误，并按 3 秒节奏继续重试。

### `POST /system/power`

- body：`{"action": "restart" | "sleep"}`，非法值返回 400。
- **`subprocess.Popen` 先 spawn（不等待命令完成；Windows 重启带 `/t 3` 缓冲、rundll32
  挂起有毫秒级延迟，实际生效在响应写出之后）再写 HTTP 响应**，spawn 抛错返回 500
  与异常信息。
- 平台命令（`subprocess.Popen`，不等待退出，避免睡眠阻塞 worker 线程）：
  - Windows 重启：`shutdown /r /t 3`（3 秒缓冲让前端收到响应并给出提示，
    无 UI 倒计时/取消——用户已确认采用最简方案）；
    Windows 睡眠：`rundll32.exe powrprof.dll,SetSuspendState 0,1,0`。
  - Linux 重启/睡眠：`systemctl reboot` / `systemctl suspend`；
    macOS：`osascript -e 'tell app "System Events" to restart'` /
    `osascript -e 'tell app "System Events" to sleep'`。非 Windows 平台为
    最佳努力，Popen 抛错时返回 500。
- 命令构造集中在独立函数（如 `power_command(action) -> list[str]`，非法动作抛
    ValueError），便于单测覆盖平台分支。

## 前端（templates/index.html）

- header 最右侧（`margin-left:auto`）加「系统」按钮（🖥 + 文字），样式
  同现有 badge 按钮类。
- 点击弹出 `#syspop` 浮层，**复用 `#queuepop` 的既有模式**：body 顶层
  `position:fixed`、按按钮矩形定位并夹紧视口、滚动/resize 重定位、
  纯点击开关（无 hover peek）、外点或 Esc 收起。
- 浮层内容自上而下：
  1. **CPU**：总体利用率大号数字 + 进度条；下方每核一行细进度条。
  2. **内存**：已用/总量（GB）+ 百分比进度条；swap 一行同样式（swap 为 0
     或不可用时隐藏）。
  3. **运行时长**：一行次要文字（如「已运行 3 天 4 小时」）。
  4. 底部操作区：重启 / 睡眠 两个按钮，次要危险色（红系描边），点击弹
     `window.confirm` 二次确认（与归档/cc-switch 现有模式一致），确认后 POST
     `/system/power`，按钮与浮层内显示执行状态（「已发送重启指令…」）。
- 数据获取：打开浮层时立即 `GET /system`；持续 503/失败时仍按 3 秒节奏重试
  （本地请求代价极低）；浮层为纯点击开关，无 hover peek（header 按钮不适用
  悬停语义）。展开期间每 3 秒自动刷新
  （`setInterval`，关闭/收起浮层即清除）；请求失败展示错误行，恢复后
  继续刷新。请求挂在进行中不叠加新请求。
- 重启后服务中断由现有 `#err` 轮询报错机制自然提示，不做特殊处理。

## 依赖

- `uv add psutil`，同步更新 CLAUDE.md「唯一依赖是 Jinja2」的描述。

## 测试

- 后端单测（unittest，mock psutil 与 subprocess）：
  - `/system` 返回字段形状与数值换算；
  - `/system/power` 合法 action 的命令构造（平台分支）、非法 action 400、
    Popen 抛错 500；
  - psutil 缺失时 `/system` 503。
- 前端复用既有浮层/对话框模式，手测覆盖（按钮定位、确认流程、自动
  刷新启停、Esc/外点关闭），不新增 node 测试。

## 明确不做（YAGNI）

- 不做关机（用户只要求重启与睡眠）。
- 不做电源操作令牌校验、倒计时与取消。
- 不做 CPU 温度、磁盘、网络等扩展指标。
- 不做独立采样线程或 SSE 推送系统指标。
