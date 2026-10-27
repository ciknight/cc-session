# 定时休眠设计

日期：2026-10-28

## 背景与目标

系统面板（见 `2026-10-27-system-panel-design.md`）目前支持立即重启/睡眠。本功能在
系统浮层中增加**定时睡眠**：设定 N 分钟后自动执行现有 sleep 动作，期间可随时取消。
到点动作复用 `power_command("sleep")`（Windows 的 `SetSuspendState`，启用休眠的
机器实际效果为休眠），不引入新的电源命令。

## 后端（monitor.py）

### 状态与计时

- `Monitor` 新增内存态定时器：`sleep_timer_lock`（专用 `threading.Lock`）、
  `sleep_timer_deadline`（unix 时间戳或 None）、`sleep_timer`（`threading.Timer`
  或 None）。不持久化，服务重启即失效（与发送队列历史一致）。
- 设置：锁内取消旧 Timer，写新 deadline，起 `threading.Timer(minutes*60,
  fire)`；fire 时锁内置 deadline 为 None 再触发命令（先清状态后执行，睡醒后
  查询不会再看到残留定时）。
- 到点执行走与 `POST /system/power` 相同的 spawn 逻辑，抽一个共享的小助手
  `spawn_power(action)`（Popen 不等待、DEVNULL、Windows CREATE_NO_WINDOW，
  OSError 抛出由调用方处理），`/system/power` 与定时 fire 共用。

### 接口

- `POST /system/sleep-timer`，body `{"minutes": <数字>}`：
  - `minutes == 0`：取消当前定时，返回 `{"ok": true, "remaining": null}`
    （无定时时取消也是幂等成功）；
  - `1 <= minutes <= 720`（整数或整数值浮点）：设定/替换定时，返回
    `{"ok": true, "remaining": <秒>}`；
  - 其他值（负数、非数字、>720）返回 400。
  - 与其他写接口一样位于 `_safe_json_write()` 之后。
- `GET /system` 响应新增 `"sleep_timer_remaining"`：整数秒（向上取整到秒）或
  `null`（无定时）。前端随既有 3 秒轮询自然刷新，不加新通道。

## 前端（templates/index.html）

系统浮层 `.sysacts` 操作区上方加一行定时控件，两种互斥状态按
`sysData.sleep_timer_remaining` 切换：

- **未设定**：预设下拉（15 / 30 / 60 / 120 分钟）+「定时睡眠」按钮；点击直接
  POST（定时可取消，不需要 confirm）。
- **已设定**：剩余时间文案（如「⏱ 29 分 41 秒后睡眠」，随 3 秒轮询更新）+
  「取消定时」按钮；点击 POST `minutes=0`。
- 请求结果沿用 `sysPop.note` 行展示成功/失败文案；浮层关闭再打开状态自然
  恢复（数据来自 `/system`）。

## 测试

- 后端单测（unittest，mock `spawn_power`/时钟）：
  - 设定后 `GET /system` 的 `sleep_timer_remaining` ≈ minutes*60；
  - 重复设定替换（30 → 15，remaining ≈ 900）；
  - `minutes=0` 取消后 remaining 为 null、Timer 被 cancel（mock Timer）；
  - 非法值（0 以外的负数、"30"、1000、缺字段）400；
  - fire 函数：deadline 清空 + 以 `"sleep"` 调用 spawn_power；
  - 接口走完整 HTTP 路径（沿用 test_management_api 的服务装置）。
- 前端复用既有模式手测（设定/倒计时显示/取消/错误文案），不加 node 测试。

## 明确不做（YAGNI）

- 不做定时重启。
- 不做服务重启后恢复定时（内存态）。
- 不做多条定时、不做指定时刻（只做相对分钟数）。
- 不做自定义分钟输入（预设四档；真需要再加）。
