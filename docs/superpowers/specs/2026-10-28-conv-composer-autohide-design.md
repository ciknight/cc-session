# 会话页发送框：移动端滚动隐藏/回弹，桌面端常驻 — 设计

日期：2026-10-28

## 需求

- 会话页（`main:has(>#conv)`）发送区在 **PC 桌面端（>768px）** 永不自动隐藏。
- **移动端（≤768px，与现有窄屏断点一致）**：`#conv` 消息区发生滚动（任意方向）时，
  发送框向下滑出隐藏；手指松开/滚动停止约 1500ms 后滑回（`scrollend` 立即回弹）。
- **隐藏时消息区要真正扩展**，为消息文字腾出更多显示空间（首版纯 transform 位移
  只滑出不释放空间，被否决）。
- **回弹不挡字**：距底在「将被发送框收回的高度」（composer 实测 offsetHeight，
  兜底 180，再加 40px 余量）以内时，回弹后同步 `scrollTop = scrollHeight` 顶到底，
  最新消息不被返回的发送框挡住；翻历史（离底远）则保持原位不打扰。
- 新建会话页（无 `#conv`）不受影响。

## 方案

`.composer` 平时保留在 flex 布局原位；隐藏时在移动端媒体查询内切换为
`position:absolute` 脱离文档流并 `transform: translateY(...)` 滑出：
`#conv`（`flex:1`）即时占满释放的空间。回弹时发送框回到文档流、滑回原位，
上方文字不动，只是底部区域重新被发送框覆盖。

### CSS（templates/index.html）

```css
main:has(>#conv) .composer{...;transition:transform .25s ease}
@media(max-width:768px){
  main:has(>#conv){position:relative}
  main:has(>#conv) .composer.conv-hide{position:absolute;left:10px;right:10px;bottom:8px;margin:0;
    transform:translateY(calc(100% + 12px))}
}
```

### JS（templates/index.html）

- `composerHtml("conv", …)` 的根节点加 `id="convcomposer"`（新建页不加），便于定位与测试。
- `wireConvAutoHide(convEl)`：在进入会话页（`showSession` 新建面板分支）时对 `#conv`
  绑定 `scroll`（passive）与 `scrollend`。
- `handleConvScroll()`：仅当 `matchMedia('(max-width:768px)').matches` 时生效——
  加 `conv-hide` 类并重置 400ms 回弹定时器；桌面端直接返回。
- `scrollend`（浏览器支持时）立即清除定时器并回弹；不支持时靠 400ms 防抖兜底。
  iOS 惯性滚动期间 scroll 事件持续触发，定时器不断重置，回弹自然等到惯性结束。
- `matchMedia` `change` 事件：视口变宽（离开移动断点）立即移除隐藏态并清定时器。
- 面板销毁/切换时 composer DOM 一并重建，隐藏态随元素消失，无需额外清理
  （定时器回调幂等：元素查不到直接返回）。
- `window.matchMedia` 不存在（旧环境/测试 stub）时整体禁用，不报错。

### 不变项

贴底跟底、加载更早的高度补偿、⚙ 浮层（`.composer.open`）、`#queuepop`（body 顶层）
均不受影响。

## 测试

`tests/test_frontend_controls.js`：

- boot() 增加可选参数注入 `window.matchMedia`，返回可控 `matches` 的 stub。
- 断言：移动端（matches=true）scroll 事件后 `convcomposer` 加 `conv-hide` 类；
  定时器触发后类移除；桌面端（matches=false）scroll 不加类。
- stub `classList` 需记录 add/remove 调用（现有 stub 为空实现，需为该节点扩展）。

## 验收

`node --test tests/test_frontend_controls.js` 与
`PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py'` 全绿。
