# 手机端会话页布局收敛与队列摘要内联 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 队列摘要内联到模型选择行右侧并把触发热区收窄到文字本身；手机端会话页头部与发送区垂直收敛，把空间还给消息区。

**Architecture:** 全部改动集中在 `templates/index.html`（结构 + CSS + 少量 JS）与 `tests/test_frontend_controls.js`。桌面布局除「队列行内联」「返回按钮同行」「计数入概览」三项普遍受益的收敛外保持不变；手机差异全部走 `@media(max-width:768px)`。次要控件用 `display:contents` 桌面展开、手机藏进 ⚙ 浮层，DOM 不移动、事件不重挂。

**Tech Stack:** 单文件 SPA（无构建）、`node --test`。

设计 spec：`docs/superpowers/specs/2026-10-25-mobile-conv-layout-design.md`

---

### Task 1: 队列摘要内联 chip

**Files:**
- Modify: `templates/index.html`（`modelControlsHtml`、`composerHtml`、`renderQueue`、CSS、main 全局点击）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 更新/新增测试**

`tests/test_frontend_controls.js` 中队列用例（原 1583 行附近）断言改 chip 结构：

```javascript
 assert.equal(b.node('queue').hidden,false,'有执行/等待时摘要 chip 可见');
 const markup=b.node('queue').innerHTML;
 assert.match(markup,/queue-summary/); assert.match(markup,/执行中 1/); assert.match(markup,/等待 2/);
```

（原 `assert.match(markup,/queue-bar/);` 一行删除，其余该用例断言不动。）

文件末尾追加结构用例：

```javascript
test('composer 结构：队列 chip 在模型行内、次要控件包进 extra、概览含计数', () => {
  const b=boot();
  const conv=b.run('composerHtml("conv",{hosted:false})');
  assert.match(conv,/queue-chip" id="queue"/);
  assert.doesNotMatch(conv,/queue-wrap/);
  assert.match(conv,/composer-extra/);
  assert.match(conv,/id="cogbtn"/);
  const fresh=b.run('composerHtml("new",{})');
  assert.match(fresh,/queue-chip" id="queue"/);
  assert.match(fresh,/composer-extra/);
  assert.match(b.run('overviewHtml({id:"a"})'),/id="convcount"/);
});
```

（`composer-extra`/`cogbtn`/`convcount` 在 Task 2/3 才实现——本用例先写全，Task 1 运行时它会因缺 `composer-extra` 失败；若想逐任务绿灯，可把后三个断言挪到 Task 2/3 的步骤里再补。执行时按此顺序：本任务先让前两条通过，其余断言随对应任务转绿。）

- [ ] **Step 2: 运行确认失败**

```
node --test tests/test_frontend_controls.js
```
预期：新用例 FAIL（无 `queue-chip`）。

- [ ] **Step 3: 实现**

`templates/index.html`：

1. `modelControlsHtml()`（现 `return \`<div class="model-controls">...\``）末尾、
   `#modelst` span 之后追加 chip：

```javascript
    <span class="mut" id="modelst" role="status"></span>
    <span class="queue-chip" id="queue" hidden title="移入或点击查看队列详情"></span></div>`;
```

2. `composerHtml` 删除独立队列行：

```javascript
    ${isNew ? "" : '<div class="queue-wrap" id="queue" hidden></div>'}
```

整行移除（注意：原 `id="queue"` 由 chip 接管，不能重复）。

3. `renderQueue` 中 `el.innerHTML = ...` 的摘要部分改为：

```javascript
  el.innerHTML = `<span class="queue-summary">${bits.join(" · ")}</span>
    <div class="queue-pop">
```

（`queue-pop` 内部结构不变。）

4. main 全局点击处理（现 2181-2182 行）：

```javascript
  const qbar = e.target.closest && e.target.closest(".queue-bar");
  if(qbar && qbar.parentElement){ qbar.parentElement.classList.toggle("open"); return; }
```

改为：

```javascript
  const chip = e.target.closest && e.target.closest(".queue-chip");
  if(chip){ chip.classList.toggle("open"); return; }
```

5. CSS：删除 `.queue-wrap{position:relative}`、`.queue-wrap:hover .queue-pop,...` 两
   条旧规则与 `.queue-bar{...}`，替换为：

```css
.queue-chip{display:inline-block;position:relative;font-size:12px;color:var(--mut);
  cursor:default;padding:2px 0}
.queue-chip .queue-pop{display:none;position:absolute;right:0;bottom:calc(100% + 6px);z-index:30;
  width:min(80vw,480px);max-height:45vh;overflow-y:auto;background:var(--bg);border:1px solid var(--bd);
  border-radius:8px;padding:8px 12px;box-shadow:0 8px 24px rgba(0,0,0,.16);text-align:left}
.queue-chip:hover .queue-pop,.queue-chip:focus-within .queue-pop,.queue-chip.open .queue-pop{display:block}
```

- [ ] **Step 4: 运行测试**

```
node --test tests/test_frontend_controls.js
```
预期：队列用例与新用例的前两条 PASS（`composer-extra` 等断言仍红，属预期）。

- [ ] **Step 5: 提交**

```
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 队列摘要内联到模型选择行并收窄触发热区"
```

---

### Task 2: 头部收敛（返回同行、cwd 让位、计数入概览）

**Files:**
- Modify: `templates/index.html`（conv-top 模板、`overviewHtml`、CSS）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 测试**

Task 1 新增用例中的 `overviewHtml` 断言（`/id="convcount"/`）本任务转绿；另把
既有 convcount 用例（623 行附近）保持不动（id 不变，仅位置移动）。

- [ ] **Step 2: 实现**

1. 会话视图模板（`load` 流程 `main.innerHTML` 的 `conv-top` 块）改为：

```javascript
          <div class="conv-top">
            <div class="conv-head">
              <button class="back" id="back">← 返回</button>
              <h2>
                <span class="b ${d.state}" id="statebadge">${STATE_ZH[d.state] || d.state}</span>
                ${d.hosted ? '<span class="chip">⚙ 托管中</span>' : ""}
                <span id="detailtitle">${esc(sessTitle(mergeMetadata(d)))}</span>
                <span class="mut conv-cwd">${esc(d.cwd || "")}</span></h2>
            </div>
            <div id="overviewwrap">${overviewHtml(mergeMetadata(d))}</div>
            <div id="interactions"></div>
            <div id="apierr" class="apierr" style="display:none"></div>
          </div>
```

（`.conv-count` 行移除；返回按钮文案「← 返回列表」改「← 返回」省宽度。）

2. `overviewHtml` summary 插入计数占位：

```javascript
function overviewHtml(d){
  return `<details class="fold conv-overview"${detailsAttrs(`info:${d.id || ""}:overview`)}>` +
    `<summary>概览 · <span id="convcount"></span>${overviewSummary(d)}</summary><div id="infopanel">${infoHtml(d)}</div></details>`;
}
```

`renderConversation` 中 `count.textContent = \`对话 (...)\`` 不变（`#convcount`
现在位于 summary 内；`applySessionDetail` 重建 overview 后 convcount 会被下一次
renderConversation 重新填上，短暂空串可接受）。

3. CSS：`.conv-top h2{...}` 改为 `.conv-head` 布局：

```css
.conv-head{display:flex;align-items:center;gap:8px}
.conv-head h2{font-size:16px;margin:8px 0 4px;flex:1;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
```

删除 `.conv-count{margin:6px 0 2px}` 规则。`@media(max-width:768px)` 内追加：

```css
  .conv-cwd{display:none}
  .conv-head h2{font-size:14px;margin:4px 0}
```

- [ ] **Step 3: 运行测试**

```
node --test tests/test_frontend_controls.js
```
预期：convcount 用例与 Task 1 新用例的 overview 断言 PASS。

- [ ] **Step 4: 提交**

```
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 会话页头部收敛为单行并合并消息计数进概览"
```

---

### Task 3: 发送区 ⚙ 收纳与 textarea 高度

**Files:**
- Modify: `templates/index.html`（`composerHtml` actions、CSS、main 全局点击）
- Test: `tests/test_frontend_controls.js`

- [ ] **Step 1: 测试**

Task 1 新增用例的 `/composer-extra/`、`/id="cogbtn"/` 断言本任务转绿。

- [ ] **Step 2: 实现**

1. `composerHtml` 的 `composer-actions` 改为：

```javascript
    <div class="composer-actions">
      <button class="back" id="sendbtn">${isNew ? "创建并发送" : "发送"}</button>
      ${isNew ? "" : `<button class="back" id="intbtn" disabled>中断并清空队列</button>`}
      <button class="back composer-cog" id="cogbtn" aria-label="发送选项"
              title="审批 / 思考 / 发送选项">⚙</button>
      <span class="composer-extra">
        <label class="ctl" id="approvalwrap">审批
          <select id="approvalsel" aria-label="托管审批模式">
            <option value="auto">自动</option>
            <option value="ask">询问</option>
            <option value="full">全部允许</option>
          </select></label>
        <label class="ctl" id="effortwrap" title="托管进程的思考档位；未托管时保存后下次托管生效">思考
          <select id="effortsel" aria-label="思考档位">
            <option value="default">默认</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
          </select></label>
        ${isNew ? "" : `<label class="ctl" title="会话显示 working 时也允许发送；非托管会话可能与终端进程并发写入">
          <input type="checkbox" id="forcecb" checked> 工作状态下也发送</label>`}
        <label class="ctl" title="勾选后本次发送会启动一个常驻 claude 进程接管该会话，之后的消息直接写进它的 stdin，不再每次重启">
          <input type="checkbox" id="hostcb" ${!isNew && d.hosted ? "checked disabled" : "checked"}> 托管（常驻进程，后续消息不再重启）</label>
        ${!isNew && d.hosted ? `<span class="chip">⚙ 已托管，消息直接写入常驻进程</span>
          <button class="back" id="unhostbtn" title="让常驻进程做完当前一轮后正常退出">退出托管</button>` : ""}
      </span>
    </div>
```

2. main 全局点击处理追加（队列 chip 分支之后）：

```javascript
  const cog = e.target.closest && e.target.closest("#cogbtn");
  if(cog){ cog.parentElement?.parentElement?.classList.toggle("open"); return; }
```

（`cogbtn` 在 `composer-actions` 内，其父级的父级即 `.composer`；mock 节点没有
`closest`，用 parentElement 链保持可测。）

3. CSS：`.composer{position:relative}`（在现有 `.composer` 规则上补），并加：

```css
.composer-extra{display:contents}
.composer-cog{display:none}
```

`@media(max-width:768px)` 内追加：

```css
  .composer-cog{display:inline-block}
  .composer-extra{display:none}
  .composer.open .composer-extra{display:flex;position:absolute;right:0;bottom:calc(100% + 6px);
    z-index:30;flex-direction:column;align-items:flex-start;gap:8px;background:var(--bg);
    border:1px solid var(--bd);border-radius:8px;padding:10px 12px;
    box-shadow:0 8px 24px rgba(0,0,0,.16);max-height:60vh;overflow-y:auto}
  .composer textarea{height:4.6em}
```

- [ ] **Step 3: 运行测试**

```
node --test tests/test_frontend_controls.js
```
预期：全部 PASS（含 Task 1 结构用例所有断言）。

- [ ] **Step 4: 提交**

```
git add templates/index.html tests/test_frontend_controls.js
git commit -m "feat: 手机端发送区次要控件收纳进齿轮浮层"
```

---

### Task 4: 全量验证与文档

- [ ] **Step 1: 全量测试**

```
PYTHONPATH=. uv run python -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_frontend_controls.js
```
预期：全 PASS（后端不涉及，跑一遍防意外）。

- [ ] **Step 2: CLAUDE.md 前端段落更新**

「前端」段落中「对话视图是聊天窗口布局」描述处补：

```markdown
固定头部单行（返回与标题同行，cwd 与消息计数并入概览折叠）；队列摘要内联为模型选择行
右侧的 chip（仅文字可 hover/点击，浮层锚定 chip）；窄屏下发送区次要控件（审批/思考/发送
选项）收进 ⚙ 浮层（`.composer-extra` 桌面 `display:contents` 原样展开），textarea 压两行。
```

（替换/合并原文中「发送队列融合进发送区（`#queue` 即摘要行容器）」的描述为 chip 语义。）

- [ ] **Step 3: 提交**

```
git add CLAUDE.md
git commit -m "docs: 更新前端布局说明（队列 chip 与手机端收敛）"
```

---

## 自查

- **Spec 覆盖**：队列内联与热区（T1）、返回同行/cwd/计数入概览（T2）、⚙ 收纳 + textarea（T3）、文档（T4）。
- **占位符**：无。
- **一致性**：`#queue` id 由 chip 接管，`renderQueue`/`loadQueue` 的 `getElementById("queue")` 不变；`.queue-chip.open` 类由 main 点击处理切换；`composer-extra` 桌面 `display:contents` 不影响现有 `#approvalsel`/`#effortsel`/`#forcecb`/`#hostcb`/`#unhostbtn` 的绑定（DOM 未移动）。
