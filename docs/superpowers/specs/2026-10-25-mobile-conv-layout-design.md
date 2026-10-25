# 手机端会话页布局收敛与队列摘要内联设计

日期：2026-10-25
状态：已确认（方案一 + 用户修正：队列摘要内联到模型选择行、热区收窄到文字本身；⚙ 收纳确认要做）

## 目标

1. 手机（≤768px）会话页把垂直空间还给消息区：头部压到约一行半，发送区常驻高度
   约减半；桌面布局基本不变。
2. 队列摘要（`▶ 执行中 N · ⏳ 等待 N`）内联到「模型选择」行右侧，不再独占一行；
   队列详情浮层的触发热区收窄到摘要文字本身（chip），不再整行响应 hover/点击。
   此项对桌面同样生效。

## 一、队列摘要内联（全部屏幕）

### 现状

- `composerHtml` 中 `#queue`（`.queue-wrap`）独占一行，位于模型选择行之上；
  `.queue-bar` 是块级元素，整行都是 hover/click 热区；浮层 `.queue-pop` 绝对定位
  锚在 `.queue-wrap` 上。手机上该行既占高度又容易误触。

### 改动（`templates/index.html`）

- `composerHtml` 去掉独立的 `#queue` 行；队列 chip 放进 `modelControlsHtml()`
  生成的行内（模型 label 之后、`#modelst` 之后）：

  ```html
  <span class="queue-chip" id="queue" hidden></span>
  ```

  chip 为 `display:inline-block; width:fit-content; position:relative`，
  仅 chip 文字区域可 hover/focus/点击。
- `renderQueue` 重写 chip 内容：摘要文字（执行中/等待/错误/通知）为 chip 的直接
  文本，`.queue-pop` 作为 chip 的子元素浮出，绝对定位 `bottom:calc(100% + 6px);
  right:0`，宽度上限 `min(80vw, 480px)`，`max-height:45vh` 保留。空闲且无错误/
  通知时 chip 仍 `hidden`。
- 全局点击兼容（现 `closest(".queue-bar")` 切换 `open` 类）改为
  `closest(".queue-chip")`；`.queue-chip.open` 仍作为触屏点击的展开态。
- hover 展开选择器从 `.queue-wrap:hover .queue-pop` 改为
  `.queue-chip:hover .queue-pop` / `:focus-within` / `.open`。

### 不做

- 队列详情浮层内容（执行中/等待/最近结束/取消按钮）不变。

## 二、头部收敛

- 「← 返回列表」按钮与标题合并同一行：`.conv-top` 内加 `<div class="conv-head">`
  包住返回按钮与 `h2`，flex 同行；返回按钮不再独占一行（桌面手机一致）。
- cwd 路径移出手机标题行：标题里的 cwd `<span>` 加 `conv-cwd` 类，≤768px 隐藏
  （cwd 信息概览/卡片仍有）。
- 「对话 (12/40)」计数行并入概览折叠条 summary：`overviewHtml` 的 summary 前缀
  `<span id="convcount">`；`renderConversation` 更新该 span；`.conv-count` 独立
  行从 `.conv-top` 移除（桌面手机一致，少一行）。

## 三、发送区收敛（≤768px）

- 次要控件收进 ⚙：composerHtml 把「审批 / 思考」两个下拉 label、「工作状态下也
  发送」「托管」两个 checkbox label、已托管 chip 与「退出托管」按钮包进
  `<span class="composer-extra">`：
  - 桌面：`.composer-extra{display:contents}`（视觉与现状完全一致，控件仍在
    `.composer-actions` 的 flex 流里，事件绑定不变）；
  - ≤768px：`.composer-extra{display:none}`，按钮行出现 ⚙ 按钮；点击给
    `.composer` 加 `open` 类，`.composer.open .composer-extra` 以绝对定位面板
    （发送区上方，同队列浮层风格）展开，含全部次要控件；再点 ⚙ 或点面板外关闭。
    控件 DOM 不移动、事件绑定不重挂。
- 发送按钮、中断按钮保留在常驻按钮行；textarea ≤768px `height` 压到约两行。
- `.composer` 加 `position:relative` 承载浮层。

## 测试（`tests/test_frontend_controls.js`）

- 队列用例更新：断言 chip 结构（`queue-chip`）与摘要文字；空闲隐藏不变。
- `composerHtml("conv")` 断言含 `composer-extra` 包裹与 ⚙ 按钮（⚙ 仅 CSS 控制
  显示，HTML 常驻）；`composerHtml("new")` 同样含 extra。
- `overviewHtml` 断言含 `convcount` span；conv-top 无独立 conv-count 行。
- CSS 断言不测（mock 无布局）。

## 风险与边界

- `display:contents` 在现代浏览器均支持；老浏览器降级为控件换行，无功能损失。
- 队列浮层改锚 chip 后，超长摘要文字 chip 会撑行——摘要本身短（计数），可接受。
- 手机上 ⚙ 面板与队列浮层同时打开的场景：各自独立开合，不做互斥。
- `renderConversation` 更新 convcount 时概览可能尚未渲染（新建会话首帧顺序），
  沿用现有 `getElementById` 空判惯例。
