"""Turn agent terminal screens and transcript files into something readable.

Two sources:
- the Claude JSONL transcript (structured, preferred, read locally);
- the terminal screen (Codex, agy, or when no transcript is found), cleaned of
  UI chrome and cut into blocks.
"""
import json
import re
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


def _harness(d):
    if d.get("isMeta") or d.get("isCompactSummary") or d.get("promptSource") == "system":
        return True
    origin = d.get("origin")
    return isinstance(origin, dict) and origin.get("kind") not in (None, "human")


def claude_chat(path, max_messages=60, tail_bytes=700_000):
    """Parse the tail of a Claude JSONL file -> (messages, model)."""
    path = Path(path)
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        data = f.read().decode("utf8", "replace")
    lines = data.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # first line is probably cut in half
    msgs, model, tools = [], "", {}
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        kind = d.get("type")
        if d.get("isSidechain") or kind not in ("user", "assistant"):
            continue
        content = (d.get("message") or {}).get("content")
        ts = d.get("timestamp", "")
        if kind == "assistant":
            model = (d.get("message") or {}).get("model") or model
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    msgs.append({"role": "assistant", "text": block["text"].strip(), "ts": ts})
                elif block.get("type") == "tool_use":
                    m = {"role": "tool", "tool": block.get("name", ""), "text": _tool_summary(block.get("name"), block.get("input")), "ts": ts}
                    tools[block.get("id")] = m
                    msgs.append(m)
        else:
            if _harness(d):
                continue
            if isinstance(content, str):
                if content.strip() and not content.lstrip().startswith(HARNESS_PREFIX):
                    msgs.append({"role": "user", "text": content.strip(), "ts": ts})
                continue
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "text" and block.get("text", "").strip() and not block["text"].lstrip().startswith(HARNESS_PREFIX):
                    msgs.append({"role": "user", "text": block["text"].strip(), "ts": ts})
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in tools:
                    res = block.get("content")
                    if isinstance(res, list):
                        res = " ".join(x.get("text", "") for x in res if isinstance(x, dict))
                    tools[block["tool_use_id"]]["result"] = str(res or "").strip()[:400]
    return msgs[-max_messages:], pretty_model(model)
