"""转录行解析：把 jsonl 行提取为前端展示用的对话消息。

与 core.handle_line 的累计状态解析不同，这里只负责 /sessions/<id>/messages
的按需提取：不维护会话状态，输出 {role, ts, parts} 结构。
"""
import json
import re

from .util import clip, _parse_iso

# 转录里 type=user 的行不一定是真人输入：工具结果回传、isMeta 注入、
# 命令/任务通知等系统文本也以 user 角色写入，展示时需区分开
SYSTEM_MARKERS = ("<task-notification>", "<command-name>", "<command-message>",
                  "<local-command-stdout>", "Caveat:", "[Request interrupted")

# 网页发送的斜杠命令在转录里是 <command-name>/<command-args> 的 user 行，
# 本地命令输出则是 <local-command-stdout> 行，解析成独立 part 供前端渲染命令气泡
COMMAND_NAME_RE = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
# 后台异步 Agent 完成时注入的 task-notification，按 tool-use-id 与发起调用配对
TASK_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>\s*([^<]+?)\s*</tool-use-id>")
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
LOCAL_STDOUT_RE = re.compile(r"^<local-command-stdout>\s*(.*?)\s*</local-command-stdout>$", re.S)


def classify_user_line(o, blocks):
    """把 type=user 的行分类。

    user      真人输入
    tool      工具结果回传
    meta      纯 boilerplate，展示和计数都应丢弃
    meta_tool isMeta + sourceToolUseID，内容属于对应工具卡片
    injected  origin/promptSource 注入的输入，不是真人输入
    system    命令输出、任务通知、中断标记等系统文本
    """
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return "tool"
    if o.get("isCompactSummary"):
        return "system"
    if o.get("isMeta"):
        return "meta_tool" if o.get("sourceToolUseID") else "meta"
    origin = o.get("origin")
    if isinstance(origin, dict):
        kind = origin.get("kind")
        # human 是真人输入；peer/coordinator/task-notification 等是注入内容
        if kind and kind != "human":
            return "injected"
    # promptSource=typed/sdk/queued 都是真人输入，只有 system 是系统注入
    if o.get("promptSource") == "system":
        return "injected"
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            if (b.get("text") or "").lstrip().startswith(SYSTEM_MARKERS):
                return "system"
    return "user"


def _result_text(b):
    """提取 tool_result 的文本内容；content 可能是字符串或 text block 列表。"""
    c = b.get("content", "")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return c if isinstance(c, str) else str(c)


def _patch_text(patch):
    """把 structuredPatch 的 hunk 列表转成统一 diff 文本。"""
    lines = []
    for hunk in patch or []:
        if not isinstance(hunk, dict):
            continue
        lines.append(f"@@ -{hunk.get('oldStart', 0)},{hunk.get('oldLines', 0)} "
                     f"+{hunk.get('newStart', 0)},{hunk.get('newLines', 0)} @@")
        lines.extend(str(x) for x in (hunk.get("lines") or []))
    return "\n".join(lines)


def parse_tool_result(o, block):
    """把 tool_result 与行级 toolUseResult 合并成可展示结果。"""
    tur = o.get("toolUseResult")
    text = _result_text(block)
    kind = "error" if block.get("is_error") else "text"
    content = block.get("content")
    if isinstance(content, list) and any(
            isinstance(x, dict) and x.get("type") == "image" for x in content):
        kind, text = "image", "图片"
    if isinstance(tur, dict):
        patch = tur.get("structuredPatch")
        if isinstance(patch, list) and patch:
            kind, text = "diff", _patch_text(patch)
        elif "stdout" in tur or "stderr" in tur:
            chunks = []
            if tur.get("stdout"):
                chunks.append(str(tur["stdout"]))
            if tur.get("stderr"):
                chunks.append("[stderr]\n" + str(tur["stderr"]))
            if chunks:
                text = "\n".join(chunks)
        elif isinstance(tur.get("file"), dict):
            f = tur["file"]
            text = f"{f.get('filePath', '')} ({f.get('numLines', '?')} 行)"
        elif tur.get("type") == "image":
            kind, text = "image", str(tur.get("path") or tur.get("filePath") or "图片")
    return {"type": "tool_result", "tool_use_id": block.get("tool_use_id"),
            "is_error": bool(block.get("is_error")), "kind": kind,
            "text": clip(text, 8000)}


def _align_line_start(f, offset, size):
    """把字节偏移向前对齐到行首，避免从半行开始解析。"""
    if offset <= 0:
        return 0
    pos = min(offset, size)
    while pos > 0:
        start = max(0, pos - 4096)
        f.seek(start)
        data = f.read(pos - start)
        idx = data.rfind(b"\n")
        if idx >= 0:
            return start + idx + 1
        pos = start
    return 0


def _parse_message_chunk(data, tools):
    """解析一段字节数据，返回 (messages, 末尾半行)。"""
    *lines, tail = data.split(b"\n")
    msgs = []
    for ln in lines:
        m = parse_msg_line(ln)
        if not m:
            continue
        keep = []
        for part in m["parts"]:
            if part.get("type") == "tool":
                if part.get("tool_use_id"):
                    tools[part["tool_use_id"]] = part
                keep.append(part)
            elif part.get("type") == "tool_result":
                target = tools.get(part.get("tool_use_id"))
                if target is not None:
                    target["result"] = part
                else:
                    keep.append(part)
            elif part.get("type") == "tool_note":
                target = tools.get(part.get("tool_use_id"))
                if target is not None:
                    target.setdefault("notes", []).append(part["text"])
                else:
                    keep.append({"type": "text", "text": part["text"]})
            else:
                keep.append(part)
        if keep:
            m["parts"] = keep
            msgs.append(m)
    return msgs, tail


def _absorb_orphan_tool_parts(msgs, tools):
    """把消息里未匹配 tool_use 的独立 tool_result / tool_note 并入工具条目。

    向前分页加载旧块后，原先因 tool_use 落在窗口外而独立展示的结果
    可以归位到对应工具卡片；被吸收后变空的消息整体移除。
    返回 (过滤后的消息列表, 吸收的 part 数)。
    """
    absorbed = 0
    for msg in msgs:
        keep = []
        for part in msg.get("parts", []):
            target = tools.get(part.get("tool_use_id")) if isinstance(part, dict) else None
            if part.get("type") == "tool_result" and target is not None:
                if target.get("result") is None:
                    target["result"] = part
                absorbed += 1
            elif part.get("type") == "tool_note" and target is not None:
                note = part.get("text", "")
                notes = target.setdefault("notes", [])
                if note not in notes:
                    notes.append(note)
                absorbed += 1
            else:
                keep.append(part)
        msg["parts"] = keep
    return [msg for msg in msgs if msg["parts"]], absorbed


def parse_msg_line(ln, clip_len=8000):
    """把一行转录解析为一条消息；无内容返回 None。

    返回 {role: user/assistant/tool/system, ts, parts: [...]}。
    parts 类型：text / tool / tool_result / tool_note / command / command_output。
    """
    if isinstance(ln, bytes):
        ln = ln.decode("utf-8", "replace")
    if '"type"' not in ln:
        return None
    try:
        o = json.loads(ln)
    except json.JSONDecodeError:
        return None
    if o.get("isSidechain"):
        return None
    typ = o.get("type")
    if typ not in ("user", "assistant"):
        return None
    content = (o.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
    kind = classify_user_line(o, blocks) if typ == "user" else "assistant"
    if kind == "meta":
        return None
    role = {"user": "user", "injected": "system", "meta_tool": "system",
            "system": "system", "tool": "tool"}.get(kind, "assistant")
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and (b.get("text") or "").strip():
            text = b["text"]
            stripped = text.lstrip()
            if kind == "system" and stripped.startswith("<command-name>"):
                m = COMMAND_NAME_RE.search(text)
                if m:
                    am = COMMAND_ARGS_RE.search(text)
                    parts.append({"type": "command",
                                  "command": clip(m.group(1), 120),
                                  "args": clip(am.group(1).strip(), 200) if am else ""})
                    continue
            if kind == "system":
                lm = LOCAL_STDOUT_RE.match(stripped)
                if lm:
                    parts.append({"type": "command_output",
                                  "text": clip(lm.group(1), 2000)})
                    continue
            if kind == "meta_tool":
                parts.append({"type": "tool_note",
                              "tool_use_id": o.get("sourceToolUseID"),
                              "text": clip(text, 3000)})
            else:
                # 系统文本（任务通知/命令输出等）截短即可，不需要全文
                parts.append({"type": "text",
                              "text": clip(text, 500 if role == "system" else clip_len)})
        elif bt == "tool_use":
            parts.append({"type": "tool", "tool_use_id": b.get("id"),
                          "name": b.get("name"), "input": clip(b.get("input") or {}, 500)})
        elif bt == "tool_result":
            parts.append(parse_tool_result(o, b))
    if parts:
        return {"role": role, "ts": _parse_iso(o.get("timestamp")), "parts": parts}
    return None
