import hashlib
import json
import re
from datetime import datetime, time

from chat import RULE, strip_ansi


DESTRUCTIVE = [
    r"\brm\b", r"\bgit\s+push\b", r"\bgh\b", r"\bcurl\b[^\n]*\s-X\s*(POST|PUT|DELETE)\b",
    r"\bssh\b", r"\bscp\b", r"\brsync\b[^\n]*(?:\S+@\S+:|\S+:\S+)",
    r"\bterraform\s+(?:apply|destroy)\b", r"\bkubectl\s+(?:delete|apply)\b", r"\baws\b",
    r"\b(?:telegram|mail|email)\b[^\n]*(?:send|message|post|gönder)",
]


def is_destructive(command, rules_path=None):
    text = str(command or "")
    for pattern in DESTRUCTIVE:
        if re.search(pattern, text, re.I):
            return True
    try:
        rules = open(rules_path, encoding="utf-8").read() if rules_path else ""
    except OSError:
        rules = ""
    for pattern in re.findall(r"- desen: `([^`]+)`", rules):
        try:
            if re.search(pattern, text, re.I):
                return True
        except re.error:
            continue
    return False


BUCKETS = {"blocked": 0, "working": 1, "done": 2, "unknown": 3, "idle": 4}
DONE_WINDOW = 3600  # a finished session counts as "recent" for an hour, then it is just idle


def bucket(status, since=None, now=None):
    if status == "done" and (since is None or (now or 0) - since > DONE_WINDOW):
        return "idle"  # finished long ago, or we never saw it finish
    return status if status in BUCKETS else "unknown"


def status_rank(session):
    """Blocked, working, recently done, idle. Newest change first within a group (pins are a client concern)."""
    group = BUCKETS.get(session.get("bucket") or session.get("status", "unknown"), 3)
    name = str(session.get("workspace_label", "")).lower()
    return (group, -(session.get("since") or 0), name)


def quiet_hours(now=None):
    now = now or datetime.now().astimezone()
    t = now.timetz().replace(tzinfo=None)
    return t >= time(22, 0) or t < time(7, 30)


class NotificationGate:
    def __init__(self, seconds=10):
        self.seconds = seconds
        self.seen = {}

    def allow(self, pane, status, now):
        key = (pane, status)
        previous = self.seen.get(key)
        if previous is not None and now - previous < self.seconds:
            return False
        self.seen[key] = now
        return True



# --- Approval screen parsing  ---
QUESTION = re.compile(r"(do you want to proceed\?|would you like to|allow .*\?|do you want to (make|create|run|allow))", re.I)
OPTION = re.compile(r"^\s*(?:[❯›>]\s*)?(\d)\.\s+(.+?)\s*$")
DIVIDER = re.compile(r"^[\s─━╌┄—-]{10,}$")
ALWAYS = re.compile(r"don['’]t ask again|always", re.I)


def parse_approval(text):
    """Return the pending approval on screen, or None.

    Keys are taken from the numbered options themselves, never assumed:
    Claude puts "No" at 3 or 4 depending on the prompt.
    """
    lines = str(text or "").splitlines()
    q = next((i for i in range(len(lines) - 1, -1, -1) if QUESTION.search(lines[i])), None)
    if q is None:
        return None
    options, seen_option, between = {}, False, []
    for line in lines[q + 1:q + 20]:
        m = OPTION.match(line)
        if not m and not seen_option:
            between.append(line)
        if not m:
            if seen_option and line.strip():
                break
            continue
        seen_option = True
        key, label = m.group(1), m.group(2)
        low = label.lower()
        if low.startswith("no"):
            options.setdefault("deny", (key, label))
        elif low.startswith("yes") and ALWAYS.search(label):
            options.setdefault("always", (key, label))
        elif low.startswith("yes") and "auto mode" not in low:
            options.setdefault("approve", (key, label))
    if "approve" not in options or "deny" not in options:
        return None
    start, found_divider = q, False
    for i in range(q - 1, max(-1, q - 25), -1):
        start = i
        if lines[i].startswith("─") and DIVIDER.match(lines[i]):
            start, found_divider = i + 1, True
            break
    shown = lines[start:q] + between
    block = "\n".join(l.rstrip() for l in shown if l.strip() and not DIVIDER.match(l)).strip()
    if not block:
        return None
    fingerprint = hashlib.sha256((block + "|" + repr(sorted(options.items()))).encode()).hexdigest()[:16]
    return {
        "command": block,
        "question": lines[q].strip(),
        "keys": {d: k for d, (k, _) in options.items()},
        "labels": {d: l for d, (_, l) in options.items()},
        "fingerprint": fingerprint,
        "cut": start == 0 and not found_divider,
    }



# --- Generic prompts: numbered choices, unnumbered trust dialog, multi-select ---
CURSORS = "❯›>➤▶●○◉"
OPT = re.compile(r"^(\d{1,2})[.)]\s+(.+)$")
HINT = re.compile(r"\s*\((esc|enter|return|tab|[a-z])\)\s*$", re.I)
CHECK = re.compile(r"^\[([ xX✔✓])\]\s*")
TRUST_HEADING = "Accessing workspace:"
TRUST_ANSWER = "Yes, I trust this folder"
FOOTER = re.compile(r"(esc to cancel|enter to (select|confirm)|↑/↓|to navigate|tab to amend)", re.I)


def _col(raw):
    return len(raw) - len(raw.lstrip(" "))


def _opt(raw):
    text = raw.strip()
    if text and text[0] in CURSORS:
        text = text[1:].strip()
    m = OPT.match(text)
    if not m:
        return None
    label = HINT.sub("", m.group(2)).strip()
    box = CHECK.match(label)
    out = {"n": int(m.group(1)), "label": CHECK.sub("", label).strip(), "desc": ""}
    if box:
        out["checked"] = box.group(1) != " "
    return out if out["label"] else None


def mention_popup(text):
    return all(w in text for w in ("All Results", "Filesystem Only", "Plugins"))


def parse_prompt(text, agent="claude"):
    """What a blocked pane is asking, in a form the phone can render.

    Returns None, or {kind: approval|choice|trust|dismiss, question, header,
    details, options: [{n,label,desc,role,keys,checked?}], multi, fingerprint}.
    `keys` is exactly what is sent for that option (digit, plus Enter where the
    tool needs it); roles for approvals are approve / always / deny.
    """
    text = strip_ansi(text)
    if mention_popup(text):
        return _finish({"kind": "dismiss", "question": "", "header": "", "details": "", "options": [], "multi": False})
    approval = parse_approval(text)
    enter = [] if agent == "claude" else ["Enter"]
    if approval:
        order = [("approve", "approve"), ("always", "always"), ("deny", "deny")]
        options = [{"n": int(approval["keys"][d]), "label": approval["labels"][d], "desc": "", "role": role, "keys": [approval["keys"][d]]}
                   for d, role in order if d in approval["keys"]]
        done = _finish({"kind": "approval", "question": approval["question"], "header": "", "details": tidy_details(approval["command"]), "hook_note": hook_note(approval["command"]), "options": options, "multi": False,
                        "cut": approval.get("cut", False)})
        done["fingerprint"] = approval["fingerprint"]  # same id the approve endpoint checks
        return done
    lines = text.splitlines()
    trust = _trust(lines)
    if trust:
        return _finish(trust)
    options, optlines, menu_col, under = [], [], None, False
    for i, raw in enumerate(lines):
        if not raw.strip():
            under = False
            continue
        if under and menu_col is not None and _col(raw) > menu_col + (1 if raw.lstrip()[0] in CURSORS else 0) + 1:
            if options and len(options[-1]["desc"]) < 160:
                options[-1]["desc"] = (options[-1]["desc"] + " " + raw.strip()).strip()
            continue
        o = _opt(raw)
        if not o:
            under = False
            continue
        if options and o["n"] <= options[-1]["n"]:
            options, optlines = [], []
        options.append(o)
        optlines.append(i)
        stripped = raw.lstrip()
        menu_col = _col(raw) + (2 if stripped[0] in CURSORS else 0)
        under = True
    if len(options) < 2:
        return None
    q, i = [], optlines[0] - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    while i >= 0 and lines[i].strip() and not RULE.match(lines[i]) and not _opt(lines[i]):
        q.insert(0, lines[i].strip())
        i -= 1
    header = ""
    if q and q[0][:1] in "☐☒☑✔□■":
        header, q = q[0][1:].strip(), q[1:]
    else:
        j = i
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and lines[j].strip()[:1] in "☐☒☑✔□■":
            header = lines[j].strip()[1:].strip()
    question = " ".join(q).strip()
    multi = any("checked" in o for o in options)
    for o in options:
        o["role"] = "choice"
        too_long = o["n"] > 9
        o["keys"] = None if too_long else ([str(o["n"])] + ([] if multi else enter))
    options = [o for o in options if o["keys"]]
    if len(options) < 2:
        return None
    return _finish({"kind": "choice", "question": question, "header": header, "details": "", "options": options, "multi": multi})


def _trust(lines):
    clean_ = [l.strip() for l in lines]
    if TRUST_HEADING not in clean_:
        return None
    start = clean_.index(TRUST_HEADING)
    rest = clean_[start + 1:]
    ans = next((i for i, l in enumerate(rest) if l.lstrip(CURSORS + " ").strip() == TRUST_ANSWER), None)
    if ans is None:
        return None
    a, b = ans, ans
    while a > 0 and rest[a - 1]:
        a -= 1
    while b + 1 < len(rest) and rest[b + 1]:
        b += 1
    rows = rest[a:b + 1]
    cur = next((i for i, l in enumerate(rows) if l[:1] in CURSORS), None)
    if cur is None:
        return None
    options = []
    for i, row in enumerate(rows):
        steps = i - cur
        options.append({"n": i + 1, "label": row.lstrip(CURSORS + " ").strip(), "desc": "", "role": "choice",
                        "keys": ["Down" if steps > 0 else "Up"] * abs(steps) + ["Enter"]})
    folder = next((l for l in rest if l.startswith(("/", "~"))), "")
    return {"kind": "trust", "question": folder, "header": "", "details": "", "options": options, "multi": False}


def tidy_details(block):
    """Drop Claude's hint line (and its wrapped tail) and the boilerplate from an approval block."""
    out, skip = [], False
    for line in str(block).splitlines():
        t = line.strip().lstrip("│┃").strip()
        if t.startswith("Tip:"):
            skip = True
            continue
        if skip and len(t) <= 30 and t[:1].islower():
            continue
        skip = False
        if re.search(r"requires approval$", t, re.I):
            continue
        if re.match(r"Hook \S+ requires confirmation", t, re.I):
            break  # everything after is the hook's own explanation, not the command
        if re.fullmatch(r"(Bash command|Run shell command)", t, re.I):
            continue
        out.append(t)
    return "\n".join(l for l in out if l)


def hook_note(block):
    """Return the first line of a hook's explanation, if present."""
    lines = [line.strip().lstrip("│┃").strip() for line in str(block).splitlines()]
    for i, line in enumerate(lines):
        if re.match(r"Hook \S+ requires confirmation", line, re.I):
            for note in lines[i + 1:]:
                note = re.sub(r"^\[[ xX✔✓]\]\s*", "", note).strip()
                if note:
                    return note[:160]
            break
    return ""


def _finish(p):
    sig = "|".join([p["kind"], p.get("question", ""), p.get("details", "")] + [f"{o['n']}:{o['label']}" for o in p["options"]])
    p["fingerprint"] = hashlib.sha256(sig.encode()).hexdigest()[:16]
    return p


# --- Usage limits ---

def _iso_epoch(value):
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_limits(key, raw, mtime=None):
    """Normalise a limits file into {id, label, updated, items: [{label, used, resets_at}]}.

    Two formats: JSON `{"h5": {"used_percentage", "resets_at"}, "d7": {...}, "ts"}`
    (5-hour and 7-day windows), or tab-separated text `name<TAB>window<TAB>NN%<TAB>ISO-reset`
    where the percentage is what is *remaining* (a bare `disabled` row is skipped).
    """
    items, updated = [], mtime
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        updated = data.get("ts") or mtime
        for k, label in (("h5", "5h"), ("d7", "7d")):
            w = data.get(k)
            if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
                items.append({"label": label, "used": round(w["used_percentage"]), "resets_at": w.get("resets_at")})
    else:
        for line in str(raw).splitlines():
            cols = [c.strip() for c in line.split("\t")]
            if len(cols) < 3 or not cols[2].endswith("%"):
                continue
            try:
                remaining = float(cols[2].rstrip("%"))
            except ValueError:
                continue
            window = "7d" if re.search(r"week", cols[1], re.I) else "5h" if re.search(r"five|5 ?h", cols[1], re.I) else cols[1]
            label = f"{cols[0]} · {window}" if cols[0] else window
            items.append({"label": label, "used": round(100 - remaining), "resets_at": _iso_epoch(cols[3]) if len(cols) > 3 else None})
    if not items:
        return None
    return {"id": key, "label": {"claude": "Claude", "agy": "agy", "codex": "Codex"}.get(key, key), "updated": updated, "items": items}


# --- Push payload ---

def _oneline(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def project_name(session):
    cwd = str(session.get("foreground_cwd") or session.get("cwd") or "").rstrip("/")
    base = cwd.rsplit("/", 1)[-1] if cwd else ""
    return base or session.get("workspace_label") or session.get("name") or session.get("pane_id", "Ulak")


PUSH_TEXT = {
    "en": {"blocked": "needs you", "done": "done", "ask": "Waiting for approval", "choice": "Waiting for a choice", "finished": "Finished"},
    "tr": {"blocked": "onay bekliyor", "done": "bitti", "ask": "Onay bekliyor", "choice": "Seçim bekliyor", "finished": "İş bitti"},
}


def build_push(session, prompt=None, destructive=False, lang="en", short=False):
    """Human push: title = project, body = the command/question or last output, tap opens the pane.

    `project` and `raw` are language-neutral (for the history list); `title`/`body` are in `lang`.
    """
    text = PUSH_TEXT.get(lang, PUSH_TEXT["en"])
    status = session.get("status")
    pane = session.get("pane_id", "")
    project = project_name(session)
    raw = ""
    if status == "blocked":
        if prompt and prompt.get("details"):
            first = [l.strip() for l in prompt["details"].splitlines() if l.strip() and not re.search(r"requires approval|^Tip:|^Bash command$", l, re.I)]
            raw = _oneline(first[-1] if first else prompt.get("question", ""), 140)
        elif prompt:
            raw = _oneline(prompt.get("question") or "", 140)
        body = raw or (text["choice"] if prompt else text["ask"])
        if destructive:
            body = "⚠ " + body
        title = f"{project} · {text['blocked']}"
    else:
        raw = _oneline(session.get("preview") or "", 140)
        body = raw or text["finished"]
        title = f"{project} · {text['done']}"
    return {"pane": pane, "status": status, "title": title, "body": body, "tag": f"ulak-{pane}", "url": f"/?pane={pane}",
            "project": project, "raw": raw, "destructive": bool(destructive)}


def parse_codex_limits(lines, mtime=None):
    """Newest `rate_limits` in a Codex rollout (JSONL lines) as a limits provider, or None."""
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            rl = json.loads(line)["payload"]["rate_limits"]
        except (ValueError, KeyError, TypeError):
            continue
        items = []
        for slot, label in (("primary", "5h"), ("secondary", "7d")):
            w = (rl or {}).get(slot)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                if w.get("window_minutes") and w["window_minutes"] not in (300, 10080):
                    label = f"{round(w['window_minutes'] / 60)}h"
                items.append({"label": label, "used": round(w["used_percent"]), "resets_at": w.get("resets_at")})
        if items:
            return {"id": "codex", "label": "Codex", "updated": mtime, "items": items}
    return None
