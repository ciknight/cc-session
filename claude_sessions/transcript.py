"""转录行解析：把 jsonl 行提取为前端展示用的对话消息。

与 core.handle_line 的累计状态解析不同，这里只负责 /sessions/<id>/messages
的按需提取：不维护会话状态，输出 {role, ts, parts} 结构。
"""
import hashlib
import json
import re

from . import config
from .util import clip, _parse_iso

# 转录里 type=user 的行不一定是真人输入：工具结果回传、isMeta 注入、
# 命令/任务通知等系统文本也以 user 角色写入，展示时需区分开
SYSTEM_MARKERS = ("<task-notification>", "<command-name>", "<command-message>",
                  "<local-command-stdout>", "Caveat:", "[Request interrupted",
                  "<session-start-hook>", "<tick>", "<goal>",
                  "<ide_opened_file>", "<ide_selection>")

# 网页发送的斜杠命令在转录里是 <command-name>/<command-args> 的 user 行，
# 本地命令输出则是 <local-command-stdout> 行，解析成独立 part 供前端渲染命令气泡
COMMAND_NAME_RE = re.compile(r"<command-name>\s*([^<]+?)\s*</command-name>")
# 后台异步 Agent 完成时注入的 task-notification，按 tool-use-id 与发起调用配对
TASK_TOOL_USE_ID_RE = re.compile(r"<tool-use-id>\s*([^<]+?)\s*</tool-use-id>")
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
LOCAL_STDOUT_RE = re.compile(r"^<local-command-stdout>\s*(.*?)\s*</local-command-stdout>$", re.S)
# 异步 Agent 的启动回执里带 agentId，用于把工具卡片链到子代理转录
AGENT_ID_RE = re.compile(r"\bagentId\s*[:=]\s*([A-Za-z0-9_-]+)")

# 工具输入只挑展示需要的字段，避免把大段 prompt / 文件内容塞进消息 payload
TOOL_INPUT_FIELDS = (
    ("command", 500), ("description", 300), ("file_path", 400), ("path", 400),
    ("pattern", 300), ("glob", 300), ("query", 300), ("url", 500),
    ("prompt", 2000), ("plan", 2000), ("subagent_type", 80), ("model", 120),
    ("skill", 120), ("subject", 200), ("taskId", 80), ("status", 40),
)


def _tool_input_data(inp):
    """提取工具输入的结构化展示字段；未知工具也能给出可读摘要。"""
    if not isinstance(inp, dict):
        return {}
    data = {}
    for key, limit in TOOL_INPUT_FIELDS:
        value = inp.get(key)
        if isinstance(value, str):
            if value:
                data[key] = clip(value, limit)
        elif isinstance(value, (int, float, bool)):
            data[key] = value
    return data


def _tool_summary(inp):
    """一行工具摘要：Bash 取命令、Edit/Read 取路径、Grep 取 pattern 等。"""
    if not isinstance(inp, dict):
        return ""
    # Agent 的 prompt 很长，优先用 description；其余工具取最具体的字段
    keys = ("command", "file_path", "path", "pattern", "glob", "query", "url",
            "description", "subject", "skill", "plan", "prompt")
    for key in keys:
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return clip(" ".join(value.split()), 160)
    return ""


def _diff_stats(patch):
    """统计 structuredPatch 的增删行数，供前端显示 +X -Y。"""
    added = removed = 0
    for hunk in patch or []:
        if not isinstance(hunk, dict):
            continue
        for line in hunk.get("lines") or []:
            text = str(line)
            if text.startswith("+") and not text.startswith("+++"):
                added += 1
            elif text.startswith("-") and not text.startswith("---"):
                removed += 1
    return {"added": added, "removed": removed}


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


def _image_part(block):
    """保留转录里的 base64 图片；不接收 SVG 或外部 URL，前端只生成受控 data URL。"""
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    media_type, data = source.get("media_type"), source.get("data")
    if not isinstance(media_type, str) or media_type not in config.IMAGE_MEDIA_TYPES \
            or not isinstance(data, str) or not data:
        return None
    return {"type": "image", "media_type": media_type, "data": data,
            "image_id": hashlib.sha256(data.encode("utf-8")).hexdigest()}


def parse_tool_result(o, block):
    """把 tool_result 与行级 toolUseResult 合并成可展示结果。"""
    tur = o.get("toolUseResult")
    text = _result_text(block)
    kind = "error" if block.get("is_error") else "text"
    content = block.get("content")
    images = ([p for p in (_image_part(x) for x in content if isinstance(x, dict)) if p]
              if isinstance(content, list) else [])
    if images:
        kind = "image"
        if not text:
            text = "图片"
    stats = None
    file_path = None
    if isinstance(tur, dict):
        f = tur.get("file") if isinstance(tur.get("file"), dict) else {}
        candidate = tur.get("path") or tur.get("filePath") or f.get("filePath")
        if isinstance(candidate, str) and candidate:
            file_path = candidate
        patch = tur.get("structuredPatch")
        if isinstance(patch, list) and patch:
            kind, text = "diff", _patch_text(patch)
            stats = _diff_stats(patch)
        elif "stdout" in tur or "stderr" in tur:
            chunks = []
            if tur.get("stdout"):
                chunks.append(str(tur["stdout"]))
            if tur.get("stderr"):
                chunks.append("[stderr]\n" + str(tur["stderr"]))
            if chunks:
                text = "\n".join(chunks)
        elif tur.get("type") == "image":
            kind = "image"
            f = tur.get("file") if isinstance(tur.get("file"), dict) else {}
            path = tur.get("path") or tur.get("filePath") or f.get("filePath")
            if path:
                text = str(path)
                file_path = str(path)
            elif not text:
                text = "图片"
        elif isinstance(tur.get("file"), dict):
            f = tur["file"]
            text = f"{f.get('filePath', '')} ({f.get('numLines', '?')} 行)"
            if isinstance(f.get("filePath"), str) and f["filePath"]:
                file_path = f["filePath"]
    result = {"type": "tool_result", "tool_use_id": block.get("tool_use_id"),
              "is_error": bool(block.get("is_error")), "kind": kind,
              "text": clip(text, 8000)}
    if stats is not None:
        result["stats"] = stats
    if isinstance(file_path, str) and file_path:
        result["file_path"] = clip(file_path, 400)
    agent_match = AGENT_ID_RE.search(text) if isinstance(text, str) else None
    if agent_match:
        result["agent_id"] = agent_match.group(1)
    if images:
        result["images"] = images
    return result


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
    parts 类型：text / thinking / tool / tool_result / tool_note / command /
    command_output / compact / compact_summary。
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
    if typ == "system":
        # 压缩边界是上下文重置的唯一持久化标记；保留给对话视图，
        # 让长会话在什么时候压缩、压缩前多大一目了然。
        if o.get("subtype") != "compact_boundary":
            return None
        meta = o.get("compactMetadata")
        meta = meta if isinstance(meta, dict) else {}
        ts = _parse_iso(o.get("timestamp"))
        pre_tokens = meta.get("preTokens")
        duration_ms = meta.get("durationMs")
        return {"role": "system", "ts": ts, "parts": [{
            "type": "compact",
            "trigger": meta.get("trigger") if isinstance(meta.get("trigger"), str) else "",
            "pre_tokens": pre_tokens if isinstance(pre_tokens, int) and not isinstance(pre_tokens, bool) else 0,
            "duration_ms": duration_ms if isinstance(duration_ms, int) and not isinstance(duration_ms, bool) else 0,
            "text": clip(o.get("content") or "Conversation compacted", 200),
            "ts": ts,
        }]}
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
            # 压缩摘要单独成卡，前端折叠展示，避免把整段摘要塞进普通系统消息
            if kind == "system" and o.get("isCompactSummary"):
                parts.append({"type": "compact_summary", "text": clip(text, 8000)})
                continue
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
        elif bt == "image":
            image = _image_part(b)
            if image:
                parts.append(image)
        elif bt == "thinking":
            thought = b.get("thinking") or b.get("text") or ""
            if isinstance(thought, str) and thought.strip():
                parts.append({"type": "thinking", "text": clip(thought, 8000)})
        elif bt == "tool_use":
            inp = b.get("input") or {}
            part = {"type": "tool", "tool_use_id": b.get("id"),
                    "name": b.get("name"), "input": clip(inp, 500)}
            if isinstance(inp, dict):
                part["input_data"] = _tool_input_data(inp)
                summary = _tool_summary(inp)
                if summary:
                    part["summary"] = summary
            parts.append(part)
        elif bt == "tool_result":
            parts.append(parse_tool_result(o, b))
    if parts:
        return {"role": role, "ts": _parse_iso(o.get("timestamp")), "parts": parts}
    return None
