import hashlib
import json
import os
import re
import subprocess
import threading
from datetime import datetime, time

from chat import RULE, strip_ansi


DESTRUCTIVE = [
    r"\brm\b", r"\bgit\s+push\b", r"\bgh\b", r"\bcurl\b[^\n]*\s(?:-X\s*|--request[\s=]+)(POST|PUT|PATCH|DELETE)\b",
    r"\bssh\b", r"\bscp\b", r"\brsync\b[^\n]*(?:\S+@\S+:|\S+:\S+)",
    r"\bterraform\s+(?:apply|destroy)\b", r"\bkubectl\s+(?:delete|apply)\b", r"\baws\b",
    r"\b(?:telegram|mail|email)\b[^\n]*(?:send|message|post|gönder)",
]


def is_destructive(command, rules_path=None):
    text = str(command or "")
    texts = (text, re.sub(r"\s*\n\s*", " ", text))  # a narrow terminal wraps a long command over several lines
    for pattern in DESTRUCTIVE:
        if any(re.search(pattern, t, re.I) for t in texts):
            return True
    try:
        rules = open(rules_path, encoding="utf-8").read() if rules_path else ""
    except OSError:
        rules = ""
    for line in rules.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        markdown = re.fullmatch(r"-\s*(?:pattern|desen):\s*`([^`]+)`\s*", line)
        pattern = markdown.group(1) if markdown else line
        try:
            if any(re.search(pattern, t, re.I) for t in texts):
                return True
        except re.error:
            continue
    return False


BUCKETS = {"blocked": 0, "working": 1, "done": 2, "unknown": 3, "idle": 4}
DONE_WINDOW = 300  # keep finished sessions prominent briefly, then return them to idle


def bucket(status, since=None, now=None):
    if status == "done" and (since is None or (now or 0) - since >= DONE_WINDOW):
        return "idle"  # finished long ago, or we never saw it finish
    return status if status in BUCKETS else "unknown"


def status_rank(session):
    """Blocked, working, recently done, idle. Latest conversation first within a group (pins are a client concern)."""
    group = BUCKETS.get(session.get("bucket") or session.get("status", "unknown"), 3)
    name = str(session.get("workspace_label", "")).lower()
    return (group, -(session.get("active") or session.get("since") or 0), name)


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
QUESTION = re.compile(r"(do you want to proceed\?|would you like to|allow .*\?|do you want to (make|create|run|allow|overwrite)|accept this file edit\?)", re.I)
OPTION = re.compile(r"^\s*(?:[❯›>]\s*)?(\d)\.\s+(.+?)\s*$")
DIVIDER = re.compile(r"^[\s─━╌┄—-]{10,}$")
ALWAYS = re.compile(r"don['’]t ask again|always", re.I)


def parse_approval(text, agent=None):
    """Return the pending approval on screen, or None.

    Keys are taken from the numbered options themselves, never assumed:
    Claude puts "No" at 3 or 4 depending on the prompt.
    """
    lines = strip_ansi(str(text or "")).splitlines()
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
        if low.startswith(("no", "reject")):
            options.setdefault("deny", (key, label))
        elif low.startswith("yes") and ALWAYS.search(label):
            options.setdefault("always", (key, label))
        elif (low.startswith("yes") or low.startswith("accept")) and "auto mode" not in low:
            options.setdefault("approve", (key, label))
    if "approve" not in options or "deny" not in options:
        return None
    start, found_divider = q, False
    # Claude draws a rule above every request, so its command is read up to that rule however tall it is.
    floor = -1 if agent == "claude" else max(-1, q - 25)
    for i in range(q - 1, floor, -1):
        start = i
        if lines[i].startswith("─") and DIVIDER.match(lines[i]):
            start, found_divider = i + 1, True
            break
    if agent == "claude" and not found_divider:
        start = max(0, q - 24)
    shown = lines[start:q] + between
    block = "\n".join(l.rstrip() for l in shown if l.strip() and not DIVIDER.match(l)).strip()
    if not block:
        return None
    question = lines[q].strip()
    signature = (block, question, sorted(options.items()))
    fingerprint = hashlib.sha256(repr(signature).encode()).hexdigest()[:16]
    return {
        "command": block,
        "question": question,
        "keys": {d: k for d, (k, _) in options.items()},
        "labels": {d: l for d, (_, l) in options.items()},
        "fingerprint": fingerprint,
        "cut": not found_divider and (agent == "claude" or start == 0),
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
    approval = parse_approval(text, agent)
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
    (5-hour and 7-day windows; `d7_<model>` or `h5_<model>` adds a window kept for one model), or tab-separated text `name<TAB>window<TAB>NN%<TAB>ISO-reset`
    where the percentage is what is *remaining* (a bare `disabled` row is skipped).
    """
    items, updated = [], mtime
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        updated = data.get("ts") or mtime
        # `h5`/`d7` are the account's windows; `d7_fable` is the same window kept for one model.
        names = {"h5": "5h", "d7": "7d"}
        windows = [(k, label, None) for k, label in names.items()]
        for k in sorted(data):
            m = re.fullmatch(r"(h5|d7)_([A-Za-z0-9][A-Za-z0-9 .-]{0,23})", str(k))
            if m:
                windows.append((k, f"{m[2][0].upper()}{m[2][1:]} · {names[m[1]]}", m[2].lower()))
        for k, label, model in windows:
            w = data.get(k)
            if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)):
                items.append({"label": label, "used": round(w["used_percentage"]), "resets_at": w.get("resets_at"), **({"model": model} if model else {})})
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


# --- slash commands -------------------------------------------------------
# The phone gets names and a one-line description. File bodies stay on the machine.
BUILTIN_SLASH = {
    "claude": ["/clear", "/compact", "/help", "/model", "/config"],
    "codex": ["/clear", "/compact", "/help", "/model", "/status"],
    "agy": ["/clear", "/help"],
}
SLASH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SLASH_LIMIT = 200


def _slash_description(path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = handle.read(4096)
    except OSError:
        return ""
    lines = head.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), len(lines))
        for line in lines[1:end]:
            key, _, value = line.partition(":")
            if key.strip() == "description":
                return " ".join(value.strip().strip("\"'").split())[:120]
        lines = lines[end + 1:]
    body = next((line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")), "")
    return " ".join(body.split())[:120]


def _slash_files(base, source, prefix=""):
    """Markdown files under base become /name, files in a subfolder become /folder:name."""
    try:
        files = sorted(p for p in base.rglob("*.md") if p.is_file())[:SLASH_LIMIT * 2]
    except OSError:
        return
    for path in files:
        parts = [*path.relative_to(base).parts[:-1], path.stem]
        if all(SLASH_PART.match(part) for part in parts):
            yield {"name": "/" + prefix + ":".join(parts), "description": _slash_description(path), "source": source}


def _slash_skills(base, source):
    try:
        folders = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return
    for folder in folders:
        manifest = folder / "SKILL.md"
        if SLASH_PART.match(folder.name) and manifest.is_file():
            yield {"name": "/" + folder.name, "description": _slash_description(manifest), "source": source}


def slash_commands(agent, cwd, home):
    """Built-in commands first, then what is installed for this user and this project. A project entry replaces a user entry of the same name."""
    from pathlib import Path
    builtin = BUILTIN_SLASH.get(agent)
    if builtin is None:
        return []
    home = Path(home)
    found = {name: {"name": name, "description": "", "source": "builtin"} for name in builtin}
    extra = []
    if agent == "claude":
        roots = [(home / ".claude", "user")]
        if cwd and Path(cwd).is_dir() and Path(cwd).resolve() != home.resolve():
            roots.append((Path(cwd) / ".claude", "project"))
        for root, source in roots:
            extra += [*_slash_files(root / "commands", source), *_slash_skills(root / "skills", source)]
    elif agent == "codex":
        extra += _slash_files(home / ".codex/prompts", "user", prefix="prompts:")
    for item in extra:
        if found.get(item["name"], {}).get("source") != "builtin":
            found.pop(item["name"], None)
            found[item["name"]] = item
    return list(found.values())[:SLASH_LIMIT]


# Looking at a working tree must not change it or run anything the repository configures.
GIT_READ = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", "-c", "color.ui=false"]


def _without_filters(cwd, env):
    """The environment that switches off every clean/smudge filter configured in the repository itself.
    Your own global filters (git-lfs and the like) stay: the repository did not choose those commands."""
    found = _git_read(cwd, ["config", "--show-scope", "--name-only", "--get-regexp", r"^filter\."], 100000, env=env)
    names = set()
    for line in (found[0].splitlines() if found else []):
        scope, _, key = line.partition("\t")
        if scope in ("local", "worktree"):
            names.add(key.rsplit(".", 1)[0])
    pairs = [(f"{name}.{key}", value) for name in sorted(names) for key, value in (("clean", ""), ("process", ""), ("required", "false"))]
    start = int(env.get("GIT_CONFIG_COUNT") or 0)
    extra = {"GIT_CONFIG_COUNT": str(start + len(pairs))}
    for n, (key, value) in enumerate(pairs, start):
        extra[f"GIT_CONFIG_KEY_{n}"], extra[f"GIT_CONFIG_VALUE_{n}"] = key, value
    return {**env, **extra}


def _git_env():
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def _git_read(cwd, args, limit, timeout=6, env=None):
    """Run a read-only git command and return at most `limit` bytes of output, or None when it fails."""
    env = env or _git_env()
    try:
        proc = subprocess.Popen(GIT_READ + args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        data = proc.stdout.read(limit + 1)
        full = len(data) <= limit
        if not full:
            proc.kill()  # enough to show; do not buffer the rest of a huge diff
        proc.stdout.close()
        code = proc.wait()
    finally:
        timer.cancel()
    if full and code != 0:
        return None
    return data[:limit].decode("utf-8", "replace"), not full


def changes(cwd, max_chars=60000, max_files=200):
    """What differs from the last commit in `cwd`: changed files and one bounded diff. Read-only."""
    out = {"repo": False, "branch": "", "files": [], "more_files": 0, "diff": "", "truncated": False}
    if not cwd or not os.path.isdir(cwd):
        return out
    env = _without_filters(cwd, _git_env())
    # A submodule has its own configuration, so only the commit it points at is compared.
    status = _git_read(cwd, ["status", "--porcelain=v1", "-z", "--branch", "--untracked-files=normal", "--ignore-submodules=dirty"], 400000, env=env)
    if status is None:
        return out
    out["repo"] = True
    entries = status[0].split("\0")
    files, skip = [], False
    for entry in entries:
        if skip:
            skip = False
            continue
        if entry.startswith("## "):
            head = entry[3:]
            out["branch"] = "" if head.startswith("HEAD (no branch)") else head.removeprefix("No commits yet on ").split("...")[0]
            continue
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        skip = x in "RC"  # a rename is followed by the old path
        files.append({"code": "?" if x == "?" else (x if x != " " else y), "path": path})
    out["files"], out["more_files"] = files[:max_files], max(0, len(files) - max_files)
    flags = ["--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=dirty"]
    diff = _git_read(cwd, ["diff", "HEAD", *flags], max_chars, env=env) or _git_read(cwd, ["diff", "--cached", *flags], max_chars, env=env)
    if diff:
        text, cut = diff
        if cut:
            text = text[:text.rfind("\n") + 1]
        out["diff"], out["truncated"] = text, cut
    return out
