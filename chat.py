"""Turn agent terminal screens and transcript files into something readable.

Two sources:
- the Claude JSONL transcript (structured, preferred, read locally);
- the terminal screen (Codex, agy, or when no transcript is found), cleaned of
  UI chrome and cut into blocks.
"""
import difflib
import json
import re
import time
from datetime import datetime
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
RULE = re.compile(r"^[\s─━╌┄╍═│┃╭╮╰╯┌┐└┘├┤┬┴┼—\-_]{6,}$")
RULE_TITLED = re.compile(r"^\s*[─━╌═]{6,}.*[─━╌═]\s*$")
BOX_EDGE = re.compile(r"^\s*[│┃]\s?|\s?[│┃]\s*$")
CHROME = [
    re.compile(p, re.I) for p in (
        r"\bctx \d+%", r"⏵⏵", r"\? for shortcuts", r"esc to interrupt", r"shift\+tab",
        r"← for agents", r"^\s*(GPT[\w.-]*|o\d[\w.-]*)\s+(low|medium|high|xhigh|default)\s*·",
        r"^\s*(?:accept-edits|auto|plan)\s*[·|]\s*Gemini[\w. -]+?\s*[·|]\s*(?:low|medium|high|xhigh|max)\b",
        r"^\s*(?:[❯›>]\s+)?Ask Codex to do anything\s*$",
        r"Update installed", r"Remote Control", r"bypass permissions", r"auto mode (on|handles)",
        r"^\s*Tip:", r"ctrl\+t to", r"Esc to cancel", r"Enter to (select|confirm)",
        r"\(shift\+tab to cycle\)", r"^\s*Queued follow-up inputs",
    )
]
EMPTY_PROMPT = re.compile(r"^\s*[❯›>]\s*$")
SPINNER = re.compile(r"^\s*[✻✳✢✶✽·*◐◓◑◒]\s+\S.*(…|\.\.\.)")
USER = re.compile(r"^\s{0,2}[❯›>]\s+(.*)$")
ASSISTANT = re.compile(r"^\s{0,2}[●•⏺]\s+(.*)$")
TOOL_OUT = re.compile(r"^\s{0,4}[⎿└├]\s+(.*)$")
TOOL_WORDS = re.compile(r"^(Ran|Read|Explored|Called|Searching|Searched|Listed|Edited|Wrote|Updated|Bash|Update|Write|Edit|Search|Fetch|Web|Task|Grep|Glob|TodoWrite|Used|Running|Reading|Writing)\b|^\w+\(.*\)?\s*$|ctrl\+o to expand")


def strip_ansi(text):
    return ANSI.sub("", str(text or ""))


def is_chrome(line):
    return bool(RULE.match(line) or RULE_TITLED.match(line) or EMPTY_PROMPT.match(line) or any(p.search(line) for p in CHROME))


def clean_lines(text):
    """Screen text -> lines without UI chrome, box borders or runs of blanks."""
    out = []
    for raw in strip_ansi(text).splitlines():
        line = raw.rstrip()
        if is_chrome(line) or SPINNER.match(line):
            if out and out[-1] != "":
                out.append("")
            continue
        line = BOX_EDGE.sub("", line) if line.lstrip().startswith(("│", "┃")) else line
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return out


def clean_transcript(text):
    return "\n".join(clean_lines(text))


def segment(lines):
    """Cut cleaned lines into [{role, text}] blocks: user, assistant, tool, text."""
    blocks = []

    def add(role, text):
        blocks.append({"role": role, "text": text})

    for line in lines:
        m = USER.match(line)
        if m:
            add("user", m.group(1))
            continue
        m = ASSISTANT.match(line)
        if m:
            body = m.group(1)
            add("tool" if TOOL_WORDS.search(body) else "assistant", body)
            continue
        m = TOOL_OUT.match(line)
        if m:
            if blocks and blocks[-1]["role"] == "tool":
                blocks[-1]["text"] += "\n" + m.group(1)
            else:
                add("tool", m.group(1))
            continue
        if line == "":
            if blocks:
                blocks[-1]["text"] += "\n"
            continue
        body = line[2:] if line.startswith("  ") else line
        if blocks:
            blocks[-1]["text"] += "\n" + body
        else:
            add("text", body)
    for b in blocks:
        b["text"] = re.sub(r"\n{3,}", "\n\n", b["text"]).strip()
    return [b for b in blocks if b["text"]]


def screen_chat(text, limit=40):
    return segment(clean_lines(text))[-limit:]


def _plain(s):
    s = re.sub(r"```[^\n]*\n?", "", s)
    s = re.sub(r"[*_`#>]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


NOISE = re.compile(r"^(Worked for|Ran |Called |Explored|Read |\+ \d+ lines|›|Ask Codex|← for agents)", re.I)


def preview_from_blocks(blocks, limit=140):
    """Last meaningful line of output: last assistant text, else last tool line."""
    for b in reversed(blocks):
        if b["role"] in ("assistant", "text") and _plain(b["text"]):
            lines = [l for l in b["text"].splitlines() if _plain(l) and not NOISE.match(_plain(l))]
            if lines:
                return _plain(lines[-1])[:limit]
    for b in reversed(blocks):
        if b["role"] == "tool":
            return _plain(b["text"].splitlines()[0])[:limit]
    return ""


def preview_from_screen(text, limit=140):
    return preview_from_blocks(screen_chat(text, 20), limit)


CLAUDE_MODEL = re.compile(r"^\s*([A-Za-z][\w. ]*?)/(?:low|medium|high|xhigh|max)\s*\|", re.M)
CODEX_MODEL = re.compile(r"^\s*(GPT[\w.-]*|o\d[\w.-]*)\s+(?:low|medium|high|xhigh|default)\s*·", re.M)
GEMINI_MODEL = re.compile(r"(Gemini[\w. -]{1,24}?)(?:\s{2,}|\s*\||$)", re.M)
AGY_MODEL = re.compile(r"(?:accept-edits|auto|plan)\s*[·|]\s*(Gemini[\w. -]+?)\s*[·|]\s*(?:low|medium|high|xhigh|max)\b", re.I)


def detect_model(text):
    raw = strip_ansi(text)
    for rx in (CLAUDE_MODEL, CODEX_MODEL):
        found = rx.findall(raw)
        if found:
            last = found[-1]
            return (last if isinstance(last, str) else last[0]).strip()
    found = AGY_MODEL.findall(raw)
    if found:
        return found[-1].strip()
    return ""


def pretty_model(model_id):
    m = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", str(model_id or ""))
    if m:
        return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}"
    return str(model_id or "")


# --- Claude JSONL transcript ---

def claude_path(cwd, session_id, base=None):
    base = Path(base) if base else Path.home() / ".claude/projects"
    if not session_id:
        return None
    if cwd:
        p = base / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"
        if p.exists():
            return p
    found = list(base.glob(f"*/{session_id}.jsonl"))
    return found[0] if found else None


def _tool_summary(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    for key in ("command", "file_path", "path", "pattern", "description", "url", "query", "prompt"):
        if isinstance(inp.get(key), str):
            v = inp[key]
            if key in ("file_path", "path"):
                v = v.rsplit("/", 1)[-1]
            return v.strip().splitlines()[0][:160] if v.strip() else ""
    return ""


HARNESS_PREFIX = ("<command-", "<local-command", "<task-notification", "<system-reminder", "Caveat:", "<user-prompt")


def typed_command(text):
    """What was typed, if this line is the harness recording a slash command; else an empty string."""
    if not text.lstrip().startswith(("<command-name>", "<command-message>")):
        return ""
    name = re.search(r"<command-name>\s*(/[^<\s]+)\s*</command-name>", text)
    if not name:
        return ""
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    return " ".join(filter(None, [name.group(1), args.group(1).strip() if args else ""]))


def _harness(d):
    if d.get("isMeta") or d.get("isCompactSummary") or d.get("promptSource") == "system":
        return True
    origin = d.get("origin")
    return isinstance(origin, dict) and origin.get("kind") not in (None, "human")


SUBAGENT_TOOLS = ("Agent", "Task")
NOTICE_RE = re.compile(r"<tool-use-id>([\w-]+)</tool-use-id>.*?<status>(\w+)</status>")


def _usable(block):
    """A content block the reader can take apart: a mapping whose text is text and whose ids are plain strings."""
    return isinstance(block, dict) and all(isinstance(block.get(key, ""), str) for key in ("type", "text", "id", "name", "tool_use_id"))


REJECTED_NOTE = "User rejected tool use"
REJECTED_TEXT = "The user doesn't want to proceed with this tool use"
DIFF_LINES, DIFF_WIDTH, DIFF_INPUT = 40, 200, 2000


def _edit_facts(name, inp):
    """What a file change did, as far as the call itself says: a capped line diff for Edit, a line count for Write."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Write" and isinstance(inp.get("content"), str):
        return {"lines": len(inp["content"].splitlines())}
    old, new = inp.get("old_string"), inp.get("new_string")
    if name != "Edit" or not isinstance(old, str) or not isinstance(new, str):
        return {}
    old, new = old.splitlines(), new.splitlines()
    if max(len(old), len(new)) > DIFF_INPUT:
        return {}
    diff, added, removed, cut = [], 0, 0, False
    for n, line in enumerate(list(difflib.unified_diff(old, new, n=2, lineterm=""))[2:]):
        if line.startswith("@@"):
            if n:
                diff.append(["…", ""])
            continue
        kind, text = line[0], line[1:]
        added += kind == "+"
        removed += kind == "-"
        cut = cut or len(text) > DIFF_WIDTH
        diff.append([kind, text[:DIFF_WIDTH]])
    facts = {"diff": diff[:DIFF_LINES], "added": added, "removed": removed}
    if cut or len(diff) > DIFF_LINES:
        facts["diff_cut"] = True
    return facts


def _outcome(block, record):
    """A tool result block -> (state, the part of its output worth reading, or None)."""
    res = block.get("content")
    if isinstance(res, list):
        res = " ".join(x.get("text", "") for x in res if isinstance(x, dict) and isinstance(x.get("text", ""), str))
    text = str(res or "").strip()
    if not block.get("is_error"):
        return "ok", text[:400]
    state = "failed"
    # Only the user's own refusal counts; a rule, a mode or a hook that blocked the call is a failure with a reason.
    if record.get("toolUseResult") == REJECTED_NOTE or text.startswith(REJECTED_TEXT):
        state = "rejected"
        text = text.partition("the user said:")[2].partition("\n\nNote: The user's next message")[0].strip()
        if not text:
            return state, None
    if len(text) > 600:  # the end of a long error is where the summary is
        text = text[:200] + "\n…\n" + text[-400:]
    return state, text


def claude_chat(path, max_messages=60, tail_bytes=700_000):
    """Parse the tail of a Claude JSONL file -> (messages, model)."""
    with Path(path).open("rb") as f:
        size = f.seek(0, 2)
        msgs, _, model = _parse(_whole_lines(f, max(0, size - tail_bytes), size))
    return msgs[-max_messages:], pretty_model(model)


ANCHOR_BYTES = 4_000_000  # the most a phone that scrolled back is sent again on each look


def claude_page(path, end=None, start=None, max_messages=60, tail_bytes=700_000):
    """A stretch of a transcript -> (messages, model, offset of the line the first one came from; 0 when nothing is older).

    With `start`, everything from that offset on (a later offset if that is out of reach); otherwise the last messages before `end`. Pages are cut between lines."""
    with Path(path).open("rb") as f:
        size = f.seek(0, 2)
        upto = None if end is None else min(end, size)
        end = size if upto is None else upto
        reach = end if upto is None else min(size, end + 1_000_000)  # a page's last calls are answered just past it
        window = tail_bytes
        while True:
            begin = max(0, end - window) if start is None else max(min(start, end), end - ANCHOR_BYTES)
            msgs, offsets, model = _parse(_whole_lines(f, begin, reach), upto)
            if begin == 0 or start is not None or len(msgs) >= min(max_messages, 20) or msgs and window >= ANCHOR_BYTES:
                break
            window *= 2  # screenshots and long tool output make lines wider than the window
    older = begin > 0
    if start is None and len(msgs) > max_messages:
        cut = offsets[-max_messages]
        keep = offsets.index(cut)
        older = older or keep > 0
        msgs, offsets = msgs[keep:], offsets[keep:]
    if start is not None and begin <= start:
        return msgs, pretty_model(model), start
    return msgs, pretty_model(model), offsets[0] if msgs and older else 0


def _whole_lines(f, begin, end):
    """(offset, text) of every line that starts at or after `begin` and before `end`."""
    f.seek(max(0, begin - 1))
    offset = f.tell()
    data = f.read(end - offset)
    if begin:  # unless the byte before was a newline, the first piece is the end of an earlier line
        first = data.find(b"\n") + 1
        if not first:
            return
        data, offset = data[first:], offset + first
    for line in data.split(b"\n"):
        if line:
            yield offset, line.decode("utf8", "replace")
        offset += len(line) + 1


def _parse(lines, upto=None):
    """Transcript lines -> (messages, the line offset of each, model id).

    Lines at or past `upto` add no message; they only settle calls left open before it. A call never settled there is "unknown"."""
    msgs, offsets, model, tools = [], [], "", {}
    last = 0
    for offset, line in lines:
        offsets.extend([last] * (len(msgs) - len(offsets)))  # what the previous line added
        last = offset
        if "<task-notification" in line:  # a background subagent stopped; the line type that carries this varies by version
            for use_id, status in NOTICE_RE.findall(line):
                if "agent" in tools.get(use_id, {}):
                    tools[use_id]["agent"]["state"] = "done" if status == "completed" else "failed"
        try:
            d = json.loads(line)
        except ValueError:
            continue
        # A line that is not shaped like a message is skipped; the rest of the conversation still shows.
        if not isinstance(d, dict) or not isinstance(d.get("message"), dict):
            continue
        kind = d.get("type")
        if d.get("isSidechain") or kind not in ("user", "assistant"):
            continue
        content = d["message"].get("content")
        if isinstance(content, list):
            content = [block for block in content if _usable(block)]
        ts = d.get("timestamp", "")
        past = upto is not None and offset >= upto
        if kind == "assistant":
            if past:
                continue
            named = d["message"].get("model")
            model = named if isinstance(named, str) and named else model
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    msgs.append({"role": "assistant", "text": block["text"].strip(), "ts": ts})
                elif block.get("type") == "tool_use" and block.get("id") not in tools:
                    m = {"role": "tool", "tool": block.get("name", ""), "text": _tool_summary(block.get("name"), block.get("input")), "ts": ts,
                         "id": block.get("id", ""), "state": "running", **_edit_facts(block.get("name"), block.get("input"))}
                    if m["tool"] in SUBAGENT_TOOLS:  # a card of its own: what it was sent to do and whether it is back
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        m["text"] = str(inp.get("description") or "").strip()[:160]
                        m["agent"] = {"kind": str(inp.get("subagent_type") or "")[:40], "state": "running"}
                    tools[block.get("id")] = m
                    msgs.append(m)
        else:
            if _harness(d):
                continue
            if isinstance(content, str):
                if past:
                    continue
                if command := typed_command(content):
                    msgs.append({"role": "command", "text": command, "ts": ts})
                elif content.strip() and not content.lstrip().startswith(HARNESS_PREFIX):
                    msgs.append({"role": "user", "text": content.strip(), "ts": ts})
                continue
            for block in content if isinstance(content, list) else []:
                if not past and block.get("type") == "text" and block.get("text", "").strip() and not block["text"].lstrip().startswith(HARNESS_PREFIX):
                    msgs.append({"role": "user", "text": block["text"].strip(), "ts": ts})
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in tools:
                    m = tools[block["tool_use_id"]]
                    if "agent" in m:
                        launch = d.get("toolUseResult")
                        if isinstance(launch, dict) and launch.get("status") == "async_launched":
                            continue  # still out there; the notice above closes it
                        m["agent"]["state"] = "failed" if block.get("is_error") else "done"
                    m["state"], result = _outcome(block, d)
                    if result is not None:
                        m["result"] = result
    offsets.extend([last] * (len(msgs) - len(offsets)))
    if upto is not None:
        for m in msgs:
            if m.get("state") == "running":
                m["state"] = "unknown"
                if m.get("agent", {}).get("state") == "running":
                    m["agent"]["state"] = "unknown"  # a page of history cannot tell whether it ever came back
    return msgs, offsets, model


SEARCH_CHUNK = 4_000_000  # a record is searched this much at a time, never whole
_FOLD = str.maketrans("İIı", "iii")


def fold(text):
    """Lower case that keeps every character where it was, with the Turkish i's as one letter."""
    return text.translate(_FOLD).lower()  # with İ gone, no character lowers to more than one


def _snippet(text, at, end, around=60):
    one = lambda s: re.sub(r"\s+", " ", s)
    before, found, after = one(text[max(0, at - around):at]).lstrip(), one(text[at:end]), one(text[end:end + around]).rstrip()
    return before + found + after, [len(before), len(before) + len(found)]


def search_transcript(path, query, limit=50, deadline=None, stop=None):
    """What the user and the assistant said that holds `query`, newest first -> (hits, whether it gave up before the start).

    The file is read from its end a piece at a time. Tool calls, their output, harness notes and subagent lines are not searched."""
    needle, hits = fold(query), []
    if not needle:
        return hits, False
    # JSON writes these differently than they are typed; a raw line cannot be asked about them.
    quick = not any(c in '"\\' or ord(c) < 32 for c in query)
    plain = query.isascii()
    with Path(path).open("rb") as f:
        end = f.seek(0, 2)
        window = SEARCH_CHUNK
        while end > 0:
            if deadline is not None and time.monotonic() > deadline or stop is not None and stop.is_set():
                return hits, True
            begin = max(0, end - window)
            f.seek(begin)
            data = f.read(end - begin)
            first = 0
            if begin:  # the first piece may be the end of a line that starts earlier; the next read takes it whole
                first = data.find(b"\n") + 1
                if not first or first == len(data):
                    window *= 2  # one line wider than the window
                    continue
            lines, offset = [], begin + first
            for piece in data[first:].split(b"\n"):
                if piece:
                    lines.append((offset, piece))
                offset += len(piece) + 1
            del data
            for offset, piece in reversed(lines):
                if stop is not None and stop.is_set():
                    return hits, True
                line = piece.decode("utf8", "replace")
                if quick and needle not in fold(line) and (plain or "\\u" not in line):
                    continue
                for m in reversed(_parse([(offset, line)])[0]):
                    if m["role"] not in ("user", "assistant"):
                        continue
                    at = fold(m["text"]).find(needle)
                    if at < 0:
                        continue
                    snippet, where = _snippet(m["text"], at, at + len(needle))
                    hits.append({"offset": offset, "end": min(offset + len(piece) + 1, end), "role": m["role"], "ts": m["ts"], "snippet": snippet, "at": where})
                    if len(hits) >= limit:
                        return hits, False
            end, window = begin + first, SEARCH_CHUNK
    return hits, False


def claude_meters(path, tail_bytes=700_000):
    """What the end of a transcript says about the conversation's size and its prompt cache. An unknown measure has no key."""
    with Path(path).open("rb") as f:
        size = f.seek(0, 2)
        lines = [line for _, line in _whole_lines(f, max(0, size - tail_bytes), size)]
    meters, since, seen = {}, None, False
    for line in reversed(lines):
        if '"usage"' not in line and "compact_boundary" not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isSidechain"):
            continue
        if d.get("subtype") == "compact_boundary":
            break  # nothing older describes the conversation as it is now
        usage = d["message"].get("usage") if d.get("type") == "assistant" and isinstance(d.get("message"), dict) else None
        if not isinstance(usage, dict):
            continue
        tokens = sum(_count(usage.get(key)) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if not tokens:  # a reply the harness made up measures nothing
            continue
        ts = _epoch(d.get("timestamp"))
        if not seen:
            seen, since = True, ts
            if ts is not None:
                meters["context"] = {"tokens": tokens, "ts": ts}
        written = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        # Both lifetimes written at once: promise only the shorter one.
        ttl = 300 if _count(written.get("ephemeral_5m_input_tokens")) else 3600 if _count(written.get("ephemeral_1h_input_tokens")) else 0
        if ttl:
            if since is not None:
                meters["cache"] = {"ttl": ttl, "since": since}
            break
    return meters


def _count(value):
    return value if type(value) is int and value > 0 else 0


def _epoch(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, OverflowError):
        return None


def _first_words(path, cwd, head_bytes=1_000_000):
    """What was asked first in a conversation held in `cwd`; empty if it is another folder's or nothing was asked."""
    with path.open("rb") as f:
        head = f.read(head_bytes)
    folder = None
    for line in head.split(b"\n"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        folder = folder or d.get("cwd")
        if folder and folder != cwd:
            return ""
        msgs = _parse([(0, line.decode("utf8", "replace"))])[0]
        if folder and msgs and msgs[0]["role"] == "user":
            return " ".join(msgs[0]["text"].split())[:80]
    return ""


def earlier_conversations(path, cwd, limit=20):
    """The other conversations this folder has had, newest first. Only plain files beside the current one."""
    found = []
    for other in Path(path).parent.glob("*.jsonl"):
        try:
            if other.name != Path(path).name and not other.is_symlink() and other.is_file():
                found.append((other.stat().st_mtime, other))
        except OSError:
            continue
    items = []
    for ts, other in sorted(found, reverse=True):
        try:
            title = _first_words(other, cwd)
        except OSError:
            continue
        if title:
            items.append({"id": other.stem, "ts": ts, "title": title})
        if len(items) >= limit:
            break
    return items
