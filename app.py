import asyncio
import subprocess
import getpass
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

import chat as transcripts
from chat import claude_chat, claude_meters, claude_page, claude_path, earlier_conversations, detect_model, search_transcript, preview_from_screen, screen_chat, SPINNER, strip_ansi
from core import CSP, FULL_MAX, NotificationGate, full_candidates, full_command, show_hidden, tidy_details, bucket, build_push, card_options, clean_subscription, hidden_chars, push_topic, strip_jpeg, is_destructive, parse_approval, parse_codex_limits, parse_limits, parse_prompt, project_name, status_rank, slash_commands, changes
from herdr_client import HerdrClient, HerdrError, HerdrUnavailable

ROOT = Path(__file__).parent
WEB = ROOT / "web"
STATE = ROOT / ".state"


def load_config():
    """Optional local config (gitignored): .secrets/config.json. Keys: rules_file, quick_targets, quick_label, limit_files, allowed_hosts."""
    try:
        return json.loads((ROOT / ".secrets/config.json").read_text())
    except (OSError, ValueError):
        return {}


CONFIG = load_config()
RULES = CONFIG.get("rules_file") or None
QUICK_TARGETS = {str(x).lower() for x in CONFIG.get("quick_targets", [])}
QUICK_LABEL = CONFIG.get("quick_label", "")
PROJECT_ROOTS = [str(x) for x in CONFIG.get("project_roots", ["~"])]  # folders whose subfolders are offered for new sessions
MIN_WORK = float(CONFIG.get("min_work_seconds", 120))  # no "finished" push for turns shorter than this


def approval_timeout(config):
    """Seconds an approval may wait before Ulak denies it. 0 (the default) means it waits for a person, forever."""
    try:
        return max(0.0, float(config.get("approval_timeout_minutes") or 0)) * 60
    except (TypeError, ValueError):
        return 0.0


APPROVAL_TIMEOUT = approval_timeout(CONFIG)
waiting = {}        # pane -> (prompt fingerprint, when Ulak first saw it waiting)
sessions = {}       # pane -> card (what the phone shows)
raw_agents = {}     # pane -> last agent record from herdr
since = {}          # pane -> {"status", "ts"}
active = {}         # pane -> when the conversation last moved
active_saved = 0.0
pending_confirm = {}
replying_panes = set()
subscribers = set()
pane_ids = set()
read_cache = {}     # pane -> (time, screen text, status, read source)
pushed = {}         # pane -> last pushed prompt fingerprint
work_started = {}   # pane -> when the current working stretch began (kept while blocked)
delivery_tasks = set()
client_presence = {} # opaque browser UUID -> last visible heartbeat timestamp
PRESENCE_TTL = 10
PRESENCE_STEP = 5    # a push held back for a watching phone looks again this often
PRESENCE_HOLD = 60   # and gives up after this long: someone who watched for a minute has seen it
SWEEP_EVERY = 60     # seconds between two looks at what Herdr really says, behind the event stream
MAX_SUBS = 20        # phones and browsers of one owner; the oldest goes first
AUDIT_KEEP = 500
DELIVERY_DELAYS = (0.4, 0.8, 1.5, 2.5, 3.0)  # when to look for the answered prompt leaving the screen
gate = NotificationGate()
recovered_at = 0.0
RECOVER_EVERY = 10  # seconds between two repairs of the event stream
client = HerdrClient()
allowed_login = os.environ.get("ULAK_ALLOWED_LOGIN", "")
ALLOWED_HOSTS = {str(x).lower() for x in CONFIG.get("allowed_hosts", [])}  # names besides localhost and *.ts.net

TOOLS = {
    "claude": {"cmd": "claude", "models": {"": "", "sonnet": " --model sonnet", "opus": " --model opus", "haiku": " --model haiku"}},
    "codex": {"cmd": "codex", "models": {"": "", "gpt-6-luna": " -m gpt-6-luna"}},
    "agy": {"cmd": "agy", "models": {"": ""}},
}
_models_cache = {}


def tool_models(name):
    """{slug: command-line suffix} for a tool; codex and agy lists are read from the tools themselves (cached 10 min)."""
    base = TOOLS[name]["models"]
    if name == "claude":
        return base
    hit = _models_cache.get(name)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    found = []
    try:
        if name == "codex":
            data = json.loads((Path.home() / ".codex/models_cache.json").read_text())
            found = [m["slug"] for m in data.get("models", []) if m.get("visibility") == "list"]
        elif name == "agy":
            out = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=15).stdout
            found = [ln.split("\t")[0].strip() for ln in out.splitlines() if "\t" in ln]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        pass  # the list is a convenience: whatever shape the tool's own cache has, the built-in names still work
    flag = " -m " if name == "codex" else " --model "
    models = {"": ""}
    models.update({s: flag + s for s in found if re.fullmatch(r"[A-Za-z0-9._-]{1,60}", s)})
    for s, v in base.items():
        models.setdefault(s, v)
    _models_cache[name] = (time.time(), models)
    return models


ALLOWED_KEYS = {"esc": "esc", "enter": "Enter", "up": "Up", "down": "Down", "left": "Left", "right": "Right", "tab": "Tab", "space": "Space",
                "y": "y", "n": "n", **{str(i): str(i) for i in range(1, 10)}}


class Action(BaseModel):
    text: str | None = None
    decision: str | None = None
    confirm_token: str | None = None
    fingerprint: str | None = None
    n: int | None = None
    keys: list[str] | None = None
    cwd: str | None = None
    tool: str | None = None
    model: str | None = None


class Presence(BaseModel):
    client_id: str
    visible: bool


# --- helpers -------------------------------------------------------------

def save_file(path, text, mode=None):
    """Write the whole file or nothing: a reader never meets half of it, and a failed write leaves the old one."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text)
    if mode is not None:
        temp.chmod(mode)
    os.replace(temp, path)


def read_json(path, kind):
    """What `path` holds if it is a `kind` (list or dict), else an empty one."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return kind()
    return data if isinstance(data, kind) else kind()


def load_since():
    since.update({pane: entry for pane, entry in read_json(STATE / "since.json", dict).items() if isinstance(entry, dict)})
    active.update({pane: ts for pane, ts in read_json(STATE / "active.json", dict).items() if isinstance(ts, (int, float))})


def touch_active(pane, ts):
    """Remember the latest sign of conversation in a pane; written to disk at most twice a minute."""
    global active_saved
    if ts > active.get(pane, 0):
        active[pane] = ts
        if time.time() - active_saved >= 30:
            try:
                save_file(STATE / "active.json", json.dumps(active))
                active_saved = time.time()
            except OSError:
                pass
    return active.get(pane, 0)


def message_time(message):
    try:
        return datetime.fromisoformat(str(message.get("ts", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def save_since():
    try:
        save_file(STATE / "since.json", json.dumps(since))
    except OSError:
        pass


def touch_since(pane, status):
    old = since.get(pane)
    if not old or old.get("status") != status:
        since[pane] = {"status": status, "ts": time.time(), "known": old is not None}
        save_since()
    return since[pane]["ts"] if since[pane].get("known", True) else None


async def broadcast(event):
    stale = []
    for q in subscribers:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            stale.append(q)
    for q in stale:
        subscribers.discard(q)


async def get_panes():
    snapshot = await client.call("session.snapshot")
    snap = snapshot.get("snapshot", snapshot)
    agent_response = await client.call("agent.list")
    agents = agent_response.get("agents", [])
    panes = {a["pane_id"] for a in agents if a.get("pane_id")}
    panes.update(p.get("pane_id") for p in snap.get("panes", []) if p.get("pane_id"))
    return snap, agents, panes


BASE_EVENTS = [{"type": "pane.created"}, {"type": "pane.agent_detected"}, {"type": "pane.closed"}]


async def resubscribe():
    subs = list(BASE_EVENTS)
    for pane in sorted(pane_ids):
        subs.append({"type": "pane.agent_status_changed", "pane_id": pane})
    await client.subscribe(subs)


async def subscribe_pane(pane):
    if pane in pane_ids:
        return
    pane_ids.add(pane)
    await resubscribe()


async def read_screen(pane, status, max_age=3.0, force=False, history=False):
    """Read passively from the visible screen; read recent history only on an explicit chat view."""
    source = "recent_unwrapped" if history and status != "blocked" else "visible"
    hit = read_cache.get(pane)
    if hit and not force and time.time() - hit[0] < max_age and hit[2] == status and hit[3] == source:
        return hit[1]
    params = {"target": pane, "source": source}
    if source == "recent_unwrapped":
        params["lines"] = 120
    try:
        result = await client.call("agent.read", params)
    except HerdrError:
        result = await client.call("agent.read", {"target": pane, "source": "visible"})
        source = "visible"
    text = result.get("read", {}).get("text", "")
    read_cache[pane] = (time.time(), text, status, source)
    return text


def pending_from_read(text, agent=None):
    parsed = parse_approval(text, agent)
    if not parsed:
        return None
    parsed["destructive"] = is_destructive(parsed["command"], RULES)
    parsed["hidden"] = hidden_chars(parsed["command"])
    parsed["reason"] = parsed["question"]
    return parsed


async def fresh_pending(pane):
    # A blocked pane only exposes its visible screen (no scrollback).
    output = await client.call("agent.read", {"target": pane, "source": "visible"})
    pending = pending_from_read(output.get("read", {}).get("text", ""), (sessions.get(pane) or {}).get("agent"))
    return await asyncio.to_thread(with_full, pane, pending)


def with_full(pane, p, shown=None):
    """A Claude request whose command ran off the screen, with the whole command when the record pins it. Anything else comes back as it was.

    `shown` is the command as the screen has it, when `p` no longer carries that. The fingerprint then covers the whole text;
    the one the screen alone gives is kept in `screen`, for the places that only ever see the screen."""
    if not p or not p.get("cut") or p.get("kind", "approval") != "approval" or (sessions.get(pane) or {}).get("agent") != "claude":
        return p
    try:
        path = transcript_of(pane)[0]
        calls = transcripts.open_calls(path) if path else None
    except (OSError, HTTPException):
        calls = None
    shown = p.get("command", "") if shown is None else shown
    full = full_command(shown, calls or [])
    if full is None:
        found = full_candidates(shown, calls or [])
        p["approvable"] = False
        p["cut_reason"] = "no_record" if calls is None else "too_long" if len(found) == 1 and len(found[0]) > FULL_MAX else "no_match"
        return p
    p["screen"] = p["fingerprint"]
    p.update(full=show_hidden(full), full_source="transcript", approvable=True,
             destructive=bool(p.get("destructive")) or is_destructive(full, RULES), hidden=sorted(set(p.get("hidden") or []) | set(hidden_chars(full))),
             fingerprint=hashlib.sha256((p["fingerprint"] + "\0" + full).encode()).hexdigest()[:16])
    return p


def on_screen(p):
    """The fingerprint the screen alone gives a prompt."""
    return p.get("screen") or p["fingerprint"]


async def fresh_prompt(pane, agent):
    output = await client.call("agent.read", {"target": pane, "source": "visible"})
    p = parse_prompt(output.get("read", {}).get("text", ""), agent)
    if p:
        p["destructive"] = bool(p["kind"] == "approval" and is_destructive(p["details"], RULES))
    return p


def discard_changed_confirmation(pane, fingerprint):
    confirmation = pending_confirm.get(pane)
    if confirmation and fingerprint not in (confirmation["fingerprint"], confirmation.get("screen")):
        pending_confirm.pop(pane, None)


@contextmanager
def exclusive_prompt_reply(pane):
    # Claim the pane before the first await so competing replies fail immediately.
    if pane in replying_panes:
        raise HTTPException(409, "A reply is already in progress")
    replying_panes.add(pane)
    try:
        yield
    finally:
        replying_panes.remove(pane)


def display_cwd(cwd):
    home = str(Path.home())
    cwd = str(cwd or "")
    return "~" + cwd[len(home):] if cwd.startswith(home) else cwd


def subtitle(agent, project):
    title = str(agent.get("terminal_title_stripped") or agent.get("terminal_title") or "")
    title = re.sub(r"^[^\w]+", "", title)
    title = re.sub(r"\s*\|\s*" + re.escape(project) + r"\s*$", "", title)
    title = re.sub(r"^" + re.escape(getpass.getuser()) + r"@\S+:.*$", "", title)
    return title.strip()


meters_cache = {}    # (path, size, mtime_ns) -> meters; the home list asks on every look, the record is read once per change


def cached_meters(path):
    """What `claude_meters` says about a record, read again only when the file has changed."""
    try:
        st = os.stat(path)
        key = (str(path), st.st_size, st.st_mtime_ns)
        found = meters_cache.get(key)
        if found is None:
            found = claude_meters(path)
            # Cards are made on several threads at once: another one may have taken the oldest key out already.
            while len(meters_cache) >= 64:
                meters_cache.pop(next(iter(meters_cache), None), None)
            meters_cache[key] = found
        return found
    except OSError:
        return {}


async def make_card(agent, force=False):
    pane = agent["pane_id"]
    status = agent.get("agent_status") or agent.get("status") or "unknown"
    if status == "working" or (since.get(pane) or {}).get("status") == "working":
        touch_active(pane, time.time())  # working now, or stopped since the last look
    ts = touch_since(pane, status)
    project = project_name(agent)
    card = {
        "pane_id": pane, "agent": agent.get("agent") or "terminal", "status": status,
        "bucket": bucket(status, ts, time.time()), "since": ts or 0, "project": project,
        "title": subtitle(agent, project), "workspace_label": sessions.get(pane, {}).get("workspace_label", ""),
        "workspace_id": agent.get("workspace_id"), "cwd": display_cwd(agent.get("cwd")), "name": agent.get("name", ""),
        "model": sessions.get(pane, {}).get("model", ""), "preview": sessions.get(pane, {}).get("preview", ""), "pending": None,
        "quick": False, "meters": {},
        # Dies with the pane: a pane id is given out again, the terminal behind it is not.
        "born": hashlib.sha256(str(agent["terminal_id"]).encode()).hexdigest()[:12] if agent.get("terminal_id") else "",
    }
    try:
        screen = await read_screen(pane, status, force=force)
    except Exception:
        screen = ""
    card["model"] = detect_model(screen) or card["model"]
    sid = (agent.get("agent_session") or {}).get("value")
    preview = ""
    if card["agent"] == "claude" and sid:
        path = claude_path(agent.get("cwd"), sid)
        if path:
            try:
                msgs, model = await asyncio.to_thread(claude_chat, path, max_messages=30, tail_bytes=250_000)
                if spoken := [m for m in msgs if m["role"] != "tool"]:
                    touch_active(pane, message_time(spoken[-1]))
                texts = [m["text"] for m in msgs if m["role"] == "assistant"]
                tools = [m for m in msgs if m["role"] == "tool"]
                if texts:
                    preview = re.sub(r"\s+", " ", re.sub(r"[*_`#>]+", "", texts[-1])).strip()[:140]
                elif tools:
                    preview = f"{tools[-1]['tool']}: {tools[-1]['text']}"[:140]
                card["transcript"] = True
                card["model"] = card["model"] or model
                card["meters"] = await asyncio.to_thread(cached_meters, path)
            except OSError:
                pass
    card["preview"] = preview or ("" if card.get("transcript") else preview_from_screen(screen)) or card["preview"]
    p = parse_prompt(screen, card["agent"])
    if p and (status == "blocked" or (card["agent"] == "agy" and p["kind"] == "approval")):
        card["status"] = "blocked"
        card["bucket"] = "blocked"
        card["pending"] = {"kind": p["kind"], "question": p["question"], "details": "\n".join(p["details"].splitlines()[-4:]),
                           "destructive": p["kind"] == "approval" and is_destructive(p["details"], RULES),
                           "fingerprint": p["fingerprint"], "cut": p.get("cut", False), "hidden": p.get("hidden", []),
                           "roles": [o["role"] for o in p["options"]], "options": card_options(p)}
    discard_changed_confirmation(pane, (card["pending"] or {}).get("fingerprint"))
    card["active"] = active.get(pane, 0)
    return card


async def refresh_pane(pane, force=True):
    agent = raw_agents.get(pane)
    if not agent:
        return None
    labels = getattr(refresh_pane, "labels", {})
    card = await make_card(agent, force=force)
    card["workspace_label"] = labels.get(agent.get("workspace_id"), card["workspace_label"])
    card["quick"] = bool(QUICK_TARGETS) and (card["workspace_label"].lower() in QUICK_TARGETS or card["project"].lower() in QUICK_TARGETS)
    sessions[pane] = card
    return card


async def current_agent(pane):
    """The agent record with the session Herdr knows now: /clear starts a new one and no event says so."""
    agent = raw_agents.get(pane, {})
    try:
        info = await client.call("agent.get", {"target": pane})
    except HerdrError:
        return agent
    named = info.get("agent") if isinstance(info.get("agent"), dict) else info
    session = named.get("agent_session")
    if not session or pane not in raw_agents or session == agent.get("agent_session"):
        return agent
    agent["agent_session"] = session
    if pane in sessions:  # what the card says belongs to the conversation that was cleared
        sessions[pane].update(preview="", model="")
        card = await refresh_pane(pane)
        if card:
            await broadcast({"type": "session", "session": card})
    return agent


async def load_all():
    snap, agents, panes = await get_panes()
    refresh_pane.labels = {w["workspace_id"]: w.get("label", "") for w in snap.get("workspaces", [])}
    raw_agents.clear()
    for a in agents:
        if a.get("pane_id"):
            raw_agents[a["pane_id"]] = a
    gone = set(sessions) - set(raw_agents)
    for pane in gone:
        sessions.pop(pane, None)
        read_cache.pop(pane, None)
        pending_confirm.pop(pane, None)
        pushed.pop(pane, None)
        work_started.pop(pane, None)
        active.pop(pane, None)
    sem = asyncio.Semaphore(6)

    async def one(pane):
        async with sem:
            return await refresh_pane(pane, force=False)
    await asyncio.gather(*(one(p) for p in raw_agents), return_exceptions=True)
    changed = panes != pane_ids  # a gone pane must leave the list too: Herdr rejects a subscription that names one
    pane_ids.intersection_update(panes)
    pane_ids.update(panes)
    if changed:
        await resubscribe()
    return sorted(sessions.values(), key=status_rank)


async def initialize():
    load_since()
    prune_uploads()
    try:
        # Subscribe to lifecycle events immediately; the first HTTP snapshot loads
        # session details after the server can already serve the app shell.
        await client.subscribe(BASE_EVENTS)
    except Exception as exc:
        print(f"Herdr initialization failed: {exc}", flush=True)
    asyncio.create_task(event_loop())
    asyncio.create_task(sweep_loop())
    if APPROVAL_TIMEOUT:
        asyncio.create_task(timeout_loop())


async def event_loop():
    while True:
        try:
            msg = await asyncio.wait_for(client.events.get(), timeout=2)
        except asyncio.TimeoutError:
            continue
        try:
            await handle_event(msg)
        except Exception as exc:
            print(f"event handling failed: {exc}", flush=True)


async def recover():
    """The event stream broke. Look at what is really there (which also subscribes afresh) and tell open phones."""
    global recovered_at
    now = time.monotonic()
    if now - recovered_at < RECOVER_EVERY:
        return
    before = set(sessions)
    try:
        cards = await load_all()
    except HerdrError:
        return  # still down; the stream will report again
    recovered_at = now
    for pane in sorted(before - set(sessions)):
        await broadcast({"type": "removed", "pane": pane})
    for card in cards:
        await broadcast({"type": "session", "session": card})


async def handle_event(msg):
    if "_error" in msg:
        return await recover()
    event = str(msg.get("event", "")).replace(".", "_")
    data = msg.get("data", {})
    pane = data.get("pane_id") or data.get("pane", {}).get("pane_id")
    if event in ("pane_created", "pane_agent_detected") and pane:
        await subscribe_pane(pane)
        try:
            info = await client.call("agent.get", {"target": pane})
            raw_agents[pane] = {**info.get("agent", info), "pane_id": pane}
            card = await refresh_pane(pane)
            if card:
                await broadcast({"type": "session", "session": card})
        except Exception:
            pass
    elif event == "pane_closed" and pane:
        sessions.pop(pane, None)
        raw_agents.pop(pane, None)
        read_cache.pop(pane, None)
        pending_confirm.pop(pane, None)
        pushed.pop(pane, None)
        work_started.pop(pane, None)
        active.pop(pane, None)
        pane_ids.discard(pane)
        await resubscribe()
        await broadcast({"type": "removed", "pane": pane})
    elif event == "pane_agent_status_changed" and pane:
        agent = raw_agents.setdefault(pane, {"pane_id": pane})
        agent.update({k: v for k, v in data.items() if k not in {"type", "pane_id"}})
        agent["agent_status"] = data.get("agent_status", "unknown")
        now = time.time()
        if agent["agent_status"] == "working":
            work_started.setdefault(pane, now)
        await current_agent(pane)
        card = await refresh_pane(pane)
        if card:
            await broadcast({"type": "session", "session": card})
            status_seen(pane, card["status"], now)


def status_seen(pane, status, now):
    """A pane is now in this state: keep the turn's clock and decide whether the phone should hear of it."""
    worked = None
    if status == "working":
        work_started.setdefault(pane, now)
    elif status == "done":
        began = work_started.pop(pane, None)
        if began is None:
            return  # the turn began before Ulak was watching, so nobody can say it was long enough to buzz for
        worked = now - began
    elif status != "blocked":
        work_started.pop(pane, None)
    if status in ("blocked", "done"):
        asyncio.create_task(push_after_grace(pane, status, worked))


async def sweep():
    """One event lost on the way would leave a question unannounced, so now and then ask Herdr what is really there.

    Only while someone could hear of it: an open phone or a push subscription.
    """
    if not subscribers and not read_subs():
        return
    before = {pane: item.get("status") for pane, item in sessions.items()}
    try:
        cards = await load_all()
    except HerdrError:
        return  # the event stream reports the outage and repairs it
    now = time.time()
    for pane in sorted(set(before) - set(sessions)):
        await broadcast({"type": "removed", "pane": pane})
    for card in cards:
        pane, status = card["pane_id"], card.get("status")
        if status == "working":
            work_started.setdefault(pane, now)
        if before.get(pane) == status:
            continue
        await broadcast({"type": "session", "session": card})
        status_seen(pane, status, now)


async def sweep_loop():
    while True:
        await asyncio.sleep(SWEEP_EVERY)
        try:
            await sweep()
        except Exception as exc:
            print(f"sweep failed: {exc}", flush=True)


def phone_watching(now):
    return any(now - seen <= PRESENCE_TTL for seen in client_presence.values())


async def push_after_grace(pane, status, worked=None):
    if status == "done" and worked is not None and worked < MIN_WORK:
        return  # a quick turn: the phone does not need to buzz
    await asyncio.sleep(10)
    # A phone that is looking does not need a buzz. But it may be locked a second later, so the push waits
    # rather than being thrown away, and goes out when the phone stops looking while the state still stands.
    held = 0
    while phone_watching(time.time()):
        if held >= PRESENCE_HOLD:
            return
        await asyncio.sleep(PRESENCE_STEP)
        held += PRESENCE_STEP
    item = sessions.get(pane)
    now = time.time()
    if not item or item.get("status") != status:
        return
    if not gate.allow(pane, status, now):
        return
    prompt, destructive = None, False
    if status == "blocked":
        try:
            prompt = await fresh_prompt(pane, item["agent"])
        except Exception:
            prompt = None
        fp = prompt["fingerprint"] if prompt else ""
        if fp and pushed.get(pane) == fp:
            return
        pushed[pane] = fp
        destructive = bool(prompt and prompt.get("destructive"))
    else:
        pushed.pop(pane, None)
    await send_push(item, prompt, destructive)


def read_subs():
    path = ROOT / ".secrets/subscriptions.json"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def write_subs(subs):
    save_file(ROOT / ".secrets/subscriptions.json", "".join(json.dumps(s) + "\n" for s in subs), mode=0o600)


def log_notification(payload):
    """History keeps language-neutral fields; the phone composes the title in its own language."""
    path = STATE / "notifications.json"
    try:
        items = read_json(path, list)
        items.append({"ts": time.time(), "pane": payload.get("pane"), "status": payload.get("status"), "project": payload.get("project"),
                      "body": payload.get("raw", ""), "destructive": payload.get("destructive", False)})
        save_file(path, json.dumps(items[-100:], ensure_ascii=False))
    except OSError:
        pass


async def deliver(sub, payload):
    """One message to one subscription -> the push service's status code (201 when it took it, None when it could not be reached)."""
    from pywebpush import WebPushException, webpush
    key = ROOT / ".secrets/vapid_private.pem"
    target = {"endpoint": sub.get("endpoint"), "keys": sub.get("keys")}
    try:
        await asyncio.to_thread(webpush, target, json.dumps(payload), str(key), {"sub": os.environ.get("ULAK_VAPID_SUBJECT", "mailto:local@localhost")},
                                ttl=3600, headers={"Urgency": "high", "Topic": push_topic(payload.get("tag", "ulak"))})
        return 201
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        print(f"Push delivery failed ({code}): {exc}", flush=True)
        return code
    except Exception as exc:
        print(f"Push delivery failed: {exc}", flush=True)
        return None


def mark_deliveries(results):
    """Write what each push service answered onto its subscription; one that is gone for good is dropped.

    The file is read again here, so a phone that subscribed while the messages were on their way is kept."""
    now = time.time()
    subs = []
    for sub in read_subs():
        if sub.get("endpoint") in results:
            code = results[sub["endpoint"]]
            if code in (404, 410):
                continue
            if code == 201:
                sub["ok_at"] = now
            else:
                sub["fail_at"], sub["fail_code"] = now, code
        subs.append(sub)
    write_subs(subs)


async def send_push(item, prompt=None, destructive=False):
    log_notification(build_push(item, prompt, destructive))
    try:
        subs = read_subs()
        if not (ROOT / ".secrets/vapid_private.pem").exists() or not subs:
            return
        results = {}
        for sub in subs:
            results[sub.get("endpoint")] = await deliver(sub, build_push(item, prompt, destructive, lang=sub.get("lang", "en")))
        mark_deliveries(results)
        print(f"Push sent: {item.get('pane_id')} {item.get('status')}", flush=True)
    except Exception as exc:
        print(f"Push delivery failed: {exc}", flush=True)


def read_audit():
    path = STATE / "audit.json"
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        items = json.loads(text)
    except ValueError:
        items = None
    if isinstance(items, list):
        return items
    try:  # the next entry would write over it, and a record of decisions is not Ulak's to throw away
        os.replace(path, path.with_name(f"audit.json.damaged-{time.time_ns()}"))
    except OSError:
        pass
    return []


def write_audit(items):
    try:
        save_file(STATE / "audit.json", json.dumps(items[-AUDIT_KEEP:], ensure_ascii=False), mode=0o600)
    except OSError:
        pass


def audit(pane, decision, outcome, *, login="", command="", destructive=False, reason="", plain=False):
    """Record who answered what and when. Refusals are kept too: they are the proof the checks ran."""
    item = sessions.get(pane) or {}
    entry = {"id": secrets.token_hex(6), "ts": time.time(), "pane": pane, "project": item.get("project", ""), "agent": item.get("agent", ""),
             "decision": decision, "outcome": outcome, "reason": reason, "command": (command or "" if plain else tidy_details(command or ""))[:600],
             "destructive": bool(destructive), "login": login, "delivered": None}
    write_audit(read_audit() + [entry])
    return entry


async def confirm_delivery(pane, fingerprint, entry_id):
    """A reply has arrived once the prompt it answered is gone from the agent's screen."""
    delivered = False
    for delay in DELIVERY_DELAYS:
        await asyncio.sleep(delay)
        try:
            now = await fresh_prompt(pane, (sessions.get(pane) or {}).get("agent") or "claude")
        except Exception:
            continue
        if not now or now["fingerprint"] != fingerprint:
            delivered = time.time()
            break
    items = read_audit()
    for entry in items:
        if entry.get("id") == entry_id:
            entry["delivered"] = delivered
    write_audit(items)
    await broadcast({"type": "receipt", "pane": pane, "delivered": bool(delivered)})


async def expire_approvals(now=None):
    """Deny approvals nobody answered in time. It can only deny, and only the prompt it has been timing."""
    if not APPROVAL_TIMEOUT:
        return []
    now = time.time() if now is None else now
    live = {pane: s["pending"]["fingerprint"] for pane, s in sessions.items()
            if s.get("status") == "blocked" and (s.get("pending") or {}).get("kind") == "approval"}
    for pane in [p for p, (fp, _) in waiting.items() if live.get(p) != fp]:
        del waiting[pane]  # answered, changed or gone: a new prompt gets its own full wait
    expired = []
    for pane, fingerprint in live.items():
        first = waiting.setdefault(pane, (fingerprint, now))[1]
        if now - first < APPROVAL_TIMEOUT or pane in replying_panes:
            continue
        try:
            with exclusive_prompt_reply(pane):
                pending = await fresh_pending(pane)
                if not pending or on_screen(pending) != fingerprint or "deny" not in pending["keys"]:
                    waiting.pop(pane, None)  # the card is stale; time whatever is really there from scratch
                    continue
                await client.call("agent.send_keys", {"target": pane, "keys": [pending["keys"]["deny"]]})
                pending_confirm.pop(pane, None)
                entry = audit(pane, "deny", "timeout", command=pending["command"], destructive=pending["destructive"],
                              reason=f"No answer in {APPROVAL_TIMEOUT / 60:g} minutes")
                track_delivery(pane, fingerprint, entry)
            waiting[pane] = (fingerprint, float("inf"))  # done; never twice for the same prompt
            expired.append(pane)
        except Exception as exc:
            print(f"Approval timeout failed for {pane}: {exc}", flush=True)
    return expired


async def timeout_loop():
    while True:
        await asyncio.sleep(20)
        try:
            for pane in await expire_approvals():
                card = await refresh_pane(pane)
                if card:
                    await broadcast({"type": "session", "session": card})
        except Exception as exc:
            print(f"Approval timeout loop: {exc}", flush=True)


def track_delivery(pane, fingerprint, entry):
    task = asyncio.create_task(confirm_delivery(pane, fingerprint, entry["id"]))
    delivery_tasks.add(task)
    task.add_done_callback(delivery_tasks.discard)


def latest_receipt(pane):
    for entry in reversed(read_audit()):
        if entry.get("pane") == pane and entry.get("outcome") == "sent":
            return {k: entry.get(k) for k in ("id", "ts", "decision", "delivered", "destructive")}
    return None


def who(request):
    return request.headers.get("tailscale-user-login", "")


@asynccontextmanager
async def lifespan(app):
    await initialize()
    yield
    await client.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")
# A phone on mobile data asks for the conversation every second or two. Event streams are left alone (starlette 0.46 and later).
app.add_middleware(GZipMiddleware, minimum_size=1000)


def host_name(value):
    try:
        return (urlsplit("//" + (value or "")).hostname or "").rstrip(".")
    except ValueError:
        return ""


def known_host(request):
    """A web page can point its own name at 127.0.0.1 and then send any header it likes, so Ulak answers only to its own names."""
    name = host_name(request.headers.get("host"))
    return name in ALLOWED_HOSTS or name in ("127.0.0.1", "::1") or name.endswith(".ts.net") or bool(name) and "." not in name and ":" not in name


def from_another_page(request):
    """Tailscale signs whatever the phone's browser sends, including a request that some other web page asked it to send."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return False  # not a browser acting for a page
    own = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
    return urlsplit(origin).netloc not in own


@app.middleware("http")
async def require_identity(request, call_next):
    path = request.url.path
    if not known_host(request):
        return JSONResponse({"detail": "Unknown host name"}, status_code=403)
    if request.method not in ("GET", "HEAD") and from_another_page(request):
        return JSONResponse({"detail": "Request from another web page refused"}, status_code=403)
    if path.startswith("/static") or path in ("/", "/manifest.webmanifest", "/sw.js", "/api/push/public"):
        response = await call_next(request)
    else:
        login = request.headers.get("tailscale-user-login")
        if not allowed_login or not login or not secrets.compare_digest(login.encode(), allowed_login.encode()):
            return JSONResponse({"detail": "Tailscale identity required"}, status_code=403)
        response = await call_next(request)
    if path.startswith("/static") or path in ("/", "/sw.js", "/manifest.webmanifest"):
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(HerdrError)
async def herdr_failed(request, exc):
    """Herdr being down or saying no is something the phone can put into words, not a crash."""
    return JSONResponse({"detail": str(exc)}, status_code=503 if isinstance(exc, HerdrUnavailable) else 502)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def sw():
    return FileResponse(WEB / "sw.js", media_type="application/javascript")


def must_exist(pane):
    if pane not in sessions and pane not in raw_agents:
        raise HTTPException(404, "Session is closed or unknown")
    return sessions.get(pane) or {"pane_id": pane, "status": "unknown", "agent": "terminal"}


async def must_be_agent(pane):
    """Ulak carries messages to agents. A pane with no agent in it is a shell, and nothing is typed into a shell."""
    item = must_exist(pane)
    if (item.get("agent") or "terminal") != "terminal":
        return item
    # Herdr can name the agent a moment after the pane appears: ask once before refusing.
    try:
        info = await client.call("agent.get", {"target": pane})
        named = info.get("agent") if isinstance(info.get("agent"), dict) else info
    except Exception:
        named = {}
    if (named.get("agent") or "terminal") == "terminal":
        raise HTTPException(409, "No agent is running in this pane; Ulak does not type into a plain shell")
    raw_agents[pane] = {**raw_agents.get(pane, {}), **named, "pane_id": pane}
    return await refresh_pane(pane) or item


def next_blocked(exclude=None):
    for s in sorted(sessions.values(), key=status_rank):
        if s["status"] == "blocked" and s["pane_id"] != exclude:
            return s["pane_id"]
    return None


@app.get("/api/sessions")
async def list_sessions():
    try:
        return await load_all()
    except HerdrError:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/sessions/{pane}/screen")
async def screen(pane: str):
    """What the agent's terminal shows right now, as text. Read-only: this path never sends a key."""
    item = must_exist(pane)
    text = strip_ansi(await read_screen(pane, item.get("status", "unknown"), max_age=1.0))
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return {"text": "\n".join(lines[-200:])[-20000:], "ts": time.time()}


@app.get("/api/sessions/{pane}/changes")
async def session_changes(pane: str):
    """Files and diff against the last commit in the session's own folder. Read-only; the phone names no path."""
    item = must_exist(pane)
    cwd = raw_agents.get(pane, {}).get("cwd") or item.get("cwd") or ""
    return {**await asyncio.to_thread(changes, cwd), "ts": time.time()}


@app.get("/api/sessions/{pane}/commands")
async def commands(pane: str):
    item = must_exist(pane)
    cwd = raw_agents.get(pane, {}).get("cwd") or item.get("cwd") or ""
    return {"commands": await asyncio.to_thread(slash_commands, item.get("agent", ""), cwd, Path.home())}


@app.get("/api/sessions/{pane}/chat")
async def chat(pane: str, limit: int = 60, anchor: int | None = Query(None, alias="from", ge=0)):
    must_exist(pane)
    agent = await current_agent(pane)
    item = must_exist(pane)
    status = item.get("status", "unknown")
    sid = (agent.get("agent_session") or {}).get("value")
    messages, source, start, meters = None, "screen", 0, {}
    if item.get("agent") == "claude" and sid:
        path = claude_path(agent.get("cwd"), sid)
        if path:
            try:
                messages, _, start = await asyncio.to_thread(claude_page, path, start=anchor, max_messages=max(10, min(limit, 200)))
                source = "transcript"
            except OSError:
                messages = None
        if path and messages is not None:
            meters = await asyncio.to_thread(cached_meters, path)
    # Long terminal history is slow to fetch from Herdr; it is only needed when there is no transcript to show.
    screen = await read_screen(pane, status, max_age=1.0, history=messages is None)
    prompt = None
    if status == "blocked":
        prompt = parse_prompt(screen, item.get("agent", "claude"))
        if prompt:
            prompt["destructive"] = bool(prompt["kind"] == "approval" and is_destructive(prompt["details"], RULES))
            if prompt.get("cut"):  # matched on the screen's own characters, before anything was tidied or written out
                raw = (parse_approval(screen, item.get("agent", "claude")) or {}).get("command", "")
                prompt = await asyncio.to_thread(with_full, pane, prompt, raw)
                prompt.pop("screen", None)
    discard_changed_confirmation(pane, (prompt or {}).get("fingerprint"))
    if messages is None:
        messages = screen_chat(screen, max(10, min(limit, 200)))
    hint = next((l.strip() for l in reversed(strip_ansi(screen).splitlines()[-12:]) if SPINNER.match(l)), "") if status == "working" else ""
    return {"messages": messages, "source": source, "prompt": prompt, "status": status, "hint": hint, "session": item, "receipt": latest_receipt(pane),
            "start": start, "conversation": sid or "", "meters": meters, "now": time.time()}


def transcript_of(pane):
    """(path, folder) of the Claude conversation open in a pane, or (None, "")."""
    item = must_exist(pane)
    agent = raw_agents.get(pane, {})
    sid = (agent.get("agent_session") or {}).get("value")
    path = claude_path(agent.get("cwd"), sid) if item.get("agent") == "claude" and sid else None
    return path, str(agent.get("cwd") or "")


@app.get("/api/sessions/{pane}/conversations")
async def conversations(pane: str):
    """Earlier conversations of the session's own folder, the ones a /clear left behind. Read-only."""
    path, cwd = transcript_of(pane)
    return {"items": await asyncio.to_thread(earlier_conversations, path, cwd) if path else []}


@app.get("/api/sessions/{pane}/history")
async def history(pane: str, before: int | None = None, conversation: str = ""):
    """Older messages. The phone names a conversation only by an id from the list above, never a path."""
    if before is not None and before < 0:
        raise HTTPException(400, "Wrong position")
    path, cwd = transcript_of(pane)
    if path and conversation:
        known = await asyncio.to_thread(earlier_conversations, path, cwd)
        path = path.parent / f"{conversation}.jsonl" if conversation in {item["id"] for item in known} else None
    if not path:
        raise HTTPException(404, "No such conversation")
    try:
        # History is settled: with no position given it ends at the end of the file, and nothing there is still running.
        messages, _, start = await asyncio.to_thread(claude_page, path, end=sys.maxsize if before is None else before)
    except OSError:
        raise HTTPException(404, "No such conversation")
    return {"messages": messages, "start": start}


SEARCH_BUDGET = 2.0  # seconds for every conversation of the folder together
SEARCH_MIN, SEARCH_MAX, SEARCH_HITS = 2, 80, 50
search_stop = threading.Event()


def search_folder(path, cwd, query, stop):
    """Search the open conversation, then the folder's earlier ones, until the hits or the time run out."""
    deadline = time.monotonic() + SEARCH_BUDGET
    hits, partial = [], False
    for conversation, title, file in [("", "", path)] + [(c["id"], c["title"], path.parent / f"{c['id']}.jsonl") for c in earlier_conversations(path, cwd)]:
        if len(hits) >= SEARCH_HITS:
            break
        try:
            size = file.stat().st_size
            found, cut = search_transcript(file, query, limit=SEARCH_HITS - len(hits), deadline=deadline, stop=stop)
        except OSError:
            continue
        partial = partial or cut
        hits += [{**hit, "conversation": conversation, "title": title, "near": not conversation and size - hit["offset"] <= transcripts.ANCHOR_BYTES} for hit in found]
    return {"hits": hits, "partial": partial}


@app.get("/api/sessions/{pane}/search")
async def search(pane: str, q: str = ""):
    """Where a word was said in this session's folder. The phone sends words, never a path; one search runs at a time."""
    global search_stop
    path, cwd = transcript_of(pane)
    q = q.strip()
    if not SEARCH_MIN <= len(q) <= SEARCH_MAX:
        raise HTTPException(400, "Search for 2 to 80 characters")
    if not path:
        raise HTTPException(409, "No conversation to search")  # 404 is kept for a session that is gone
    search_stop.set()
    search_stop = stop = threading.Event()
    return await asyncio.to_thread(search_folder, path, cwd, q, stop)


@app.post("/api/sessions/{pane}/prompt")
async def prompt(pane: str, action: Action):
    must_exist(pane)
    if not action.text or not action.text.strip():
        raise HTTPException(400, "Message is empty")
    await must_be_agent(pane)
    sent = await client.call("agent.prompt", {"target": pane, "text": action.text})
    touch_active(pane, time.time())
    return sent


@app.get("/api/config")
async def config():
    return {"quick_label": QUICK_LABEL if QUICK_TARGETS else "", "approval_timeout_minutes": APPROVAL_TIMEOUT / 60}


@app.post("/api/quick")
async def to_quick(action: Action):
    target = next((s for s in sessions.values() if s.get("quick")), None)
    if not target:
        raise HTTPException(404, "Quick-send target not found")
    if not action.text or not action.text.strip():
        raise HTTPException(400, "Message is empty")
    await must_be_agent(target["pane_id"])
    await client.call("agent.prompt", {"target": target["pane_id"], "text": action.text})
    return {"ok": True, "pane": target["pane_id"]}


@app.post("/api/sessions/{pane}/approve")
async def approve(pane: str, action: Action, request: Request):
    item = sessions.get(pane)
    if not item or pane not in pane_ids:
        raise HTTPException(404, "Session is closed or unknown")
    if action.decision not in ("approve", "deny", "always"):
        raise HTTPException(400, "decision must be approve, deny, or always")
    seen = {}  # what was on screen, so a refusal is recorded with its command
    try:
        if item.get("status") != "blocked":
            discard_changed_confirmation(pane, None)
            raise HTTPException(409, "Session is no longer waiting for approval")
        with exclusive_prompt_reply(pane):
            return await _approve(pane, item, action, request, seen)
    except HTTPException as exc:
        audit(pane, action.decision, "refused", login=who(request), reason=str(exc.detail), **seen)
        raise


async def _approve(pane, item, action, request, seen):
    try:
        pending = await fresh_pending(pane)
    except HerdrUnavailable as exc:
        raise HTTPException(503, str(exc))
    except Exception:
        pending = None
    discard_changed_confirmation(pane, (pending or {}).get("fingerprint"))
    whole = bool((pending or {}).get("full"))  # read from the record: it goes on the audit record as it is, from its first character
    said = (pending or {}).get("full") or (pending or {}).get("command", "")
    seen.update(command=said, destructive=bool((pending or {}).get("destructive")), plain=whole)
    if not pending:
        raise HTTPException(409, "No recognised approval on screen; use the terminal")
    # A refusal needs no more than the screen; a yes to a command read from the record must name that whole command.
    known = [pending["fingerprint"]] + ([on_screen(pending)] if action.decision == "deny" else [])
    if not action.fingerprint or not any(secrets.compare_digest(action.fingerprint, one) for one in known):
        raise HTTPException(409, "The approval screen changed; refresh")
    if action.decision not in pending["keys"]:
        raise HTTPException(409, "This prompt has no such option")
    if pending.get("cut") and not pending.get("approvable") and item.get("agent") == "claude" and action.decision != "deny":
        raise HTTPException(409, "The full command is not visible; it can only be denied")
    if action.decision == "always" and whole:
        raise HTTPException(400, "Session-wide allow is disabled for a command read from the record")
    if action.decision == "always" and pending["destructive"]:
        raise HTTPException(400, "Session-wide allow is disabled for destructive commands")
    if action.decision == "always" and pending["hidden"]:
        raise HTTPException(400, "Session-wide allow is disabled for commands with characters that cannot be seen")
    needs_confirmation = pending["destructive"] and action.decision == "approve"
    if action.confirm_token is not None:
        confirmation = pending_confirm.get(pane)
        valid = (
            needs_confirmation and confirmation
            and confirmation["expires"] > time.time()
            and confirmation["decision"] == action.decision
            and confirmation["fingerprint"] == pending["fingerprint"]
            and action.confirm_token.isascii()
            and secrets.compare_digest(action.confirm_token, confirmation["token"])
        )
        if not valid:
            raise HTTPException(409, "The confirmation is no longer valid; try again")
    elif needs_confirmation:
        token = secrets.token_urlsafe(24)
        pending_confirm[pane] = {"token": token, "decision": action.decision, "fingerprint": pending["fingerprint"], "screen": on_screen(pending), "expires": time.time() + 120}
        audit(pane, action.decision, "confirm_requested", login=who(request), command=said, destructive=True, plain=whole)
        return {"confirm_required": True, "confirm_token": token, "command": said if whole else tidy_details(said)}
    pending_confirm.pop(pane, None)
    await client.call("agent.send_keys", {"target": pane, "keys": [pending["keys"][action.decision]]})
    entry = audit(pane, action.decision, "sent", login=who(request), command=said, destructive=pending["destructive"], plain=whole)
    track_delivery(pane, on_screen(pending), entry)
    return {"ok": True, "next": next_blocked(pane)}


@app.post("/api/sessions/{pane}/choose")
async def choose(pane: str, action: Action, request: Request):
    item = sessions.get(pane)
    if not item or pane not in pane_ids:
        raise HTTPException(404, "Session is closed or unknown")
    seen = {}  # the question on screen, so a refused choice is recorded with it
    try:
        if item.get("status") != "blocked":
            raise HTTPException(409, "Session is no longer waiting for anything")
        with exclusive_prompt_reply(pane):
            p = await fresh_prompt(pane, item.get("agent", "claude"))
            if not p or p["kind"] in ("approval", "dismiss"):
                raise HTTPException(409, "No menu to choose from on screen")
            seen["command"] = p.get("question", "")
            if not action.fingerprint or not secrets.compare_digest(action.fingerprint, p["fingerprint"]):
                raise HTTPException(409, "The screen changed; refresh")
            option = next((o for o in p["options"] if o["n"] == action.n), None)
            if not option:
                raise HTTPException(409, "No such option")
            pending_confirm.pop(pane, None)
            await client.call("agent.send_keys", {"target": pane, "keys": option["keys"]})
            entry = audit(pane, "choose", "sent", login=who(request), command=f"{p.get('question', '')}\n> {option['label']}".strip(), plain=True)
            track_delivery(pane, p["fingerprint"], entry)
    except HTTPException as exc:
        audit(pane, "choose", "refused", login=who(request), reason=str(exc.detail), plain=True, **seen)
        raise
    return {"ok": True, "next": next_blocked(pane)}


@app.post("/api/sessions/{pane}/keys")
async def send_keys(pane: str, action: Action, request: Request):
    item = must_exist(pane)
    keys = [ALLOWED_KEYS.get(str(k).lower()) for k in (action.keys or [])]
    if not keys or len(keys) > 4 or None in keys:
        raise HTTPException(400, "Invalid key")
    item = await must_be_agent(pane)
    if pane in replying_panes and item.get("status") == "blocked":
        audit(pane, "keys", "refused", login=who(request), command=f"> {' '.join(keys)}", reason="A reply is already in progress", plain=True)
    with exclusive_prompt_reply(pane):
        waiting = None  # keys typed at a waiting agent answer it, so they go on the record like any decision
        if item.get("status") == "blocked":
            pending = await fresh_pending(pane)
            try:
                waiting = pending or await fresh_prompt(pane, item.get("agent") or "claude")
            except Exception:
                waiting = pending
            asked = (pending or {}).get("command") or (waiting or {}).get("question", "")
            typed = f"{asked}\n> {' '.join(keys)}".strip()
            cut = bool(pending and pending["cut"] and item.get("agent") == "claude")
            if pending and (pending["destructive"] or cut):
                # Only keys that cannot say yes: a list of the dangerous ones would miss an option the prompt adds.
                harmless = {"esc", "Up", "Down", "Left", "Right", "Tab", "n", pending["keys"]["deny"]}
                if any(k not in harmless for k in keys):
                    reason = ("The full command is not visible; it can only be denied" if cut
                              else "Destructive command: approve from the approval card (two steps)")
                    audit(pane, "keys", "refused", login=who(request), command=typed, destructive=pending["destructive"], reason=reason, plain=True)
                    raise HTTPException(409, reason)
        pending_confirm.pop(pane, None)
        await client.call("agent.send_keys", {"target": pane, "keys": keys})
        if item.get("status") == "blocked":
            entry = audit(pane, "keys", "sent", login=who(request), command=typed, destructive=bool((pending or {}).get("destructive")), plain=True)
            if waiting and waiting.get("fingerprint"):
                track_delivery(pane, on_screen(waiting), entry)
    return {"ok": True}


@app.post("/api/sessions/{pane}/close")
async def close_session(pane: str, action: Action):
    item = must_exist(pane)
    entry = pending_confirm.get(("close", pane))
    valid = bool(entry and entry["expires"] > time.time() and secrets.compare_digest(action.confirm_token or "", entry["token"]))
    if not valid:
        token = secrets.token_urlsafe(18)
        pending_confirm[("close", pane)] = {"token": token, "expires": time.time() + 60}
        return {"confirm_required": True, "confirm_token": token, "working": item.get("status") == "working", "project": item.get("project", pane)}
    pending_confirm.pop(("close", pane), None)
    await client.call("pane.close", {"pane_id": pane})
    return {"ok": True}


def project_dirs():
    home = Path.home()
    seen, out = set(), []
    for s in sessions.values():
        c = str(s.get("cwd", "")).replace("~", str(home), 1)
        if c and c not in seen and Path(c).is_dir():
            seen.add(c)
            out.append(c)
    for base in (Path(os.path.expanduser(r)) for r in PROJECT_ROOTS):
        try:
            for p in sorted(base.iterdir()):
                if p.is_dir() and not p.name.startswith(".") and str(p) not in seen:
                    seen.add(str(p))
                    out.append(str(p))
        except OSError:
            pass
    return out


@app.get("/api/projects")
async def projects():
    return {"projects": [{"path": p, "name": Path(p).name, "display": display_cwd(p)} for p in project_dirs()],
            "tools": {k: [m for m in await asyncio.to_thread(tool_models, k) if m] for k in TOOLS}}


@app.post("/api/sessions")
async def start_session(action: Action):
    tool = TOOLS.get(action.tool or "")
    models = await asyncio.to_thread(tool_models, action.tool) if tool else {}
    if not tool or (action.model or "") not in models:
        raise HTTPException(400, "Invalid tool or model")
    cwd = os.path.realpath(os.path.expanduser(action.cwd or ""))
    home = os.path.realpath(str(Path.home()))
    if not (cwd == home or cwd.startswith(home + "/")) or not os.path.isdir(cwd) or any(part.startswith(".") for part in Path(cwd[len(home):]).parts):
        raise HTTPException(400, "Invalid folder")
    label = re.sub(r"[^A-Za-z0-9_-]", "", f"{Path(cwd).name}-{action.tool}")[:40] or "session"
    created = await client.call("workspace.create", {"cwd": cwd, "label": label, "focus": False})
    pane = created["root_pane"]["pane_id"]
    command = tool["cmd"] + models[action.model or ""]
    await client.call("pane.send_text", {"pane_id": pane, "text": command})
    await client.call("pane.send_keys", {"pane_id": pane, "keys": ["Enter"]})
    return {"ok": True, "pane": pane}


@app.get("/api/events")
async def events(request: Request):
    q = asyncio.Queue(maxsize=200)
    subscribers.add(q)

    async def stream():
        try:
            yield "retry: 2000\nevent: ready\ndata: {}\n\n"
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(q.get(), 15)
                    yield "data: " + json.dumps(payload) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            subscribers.discard(q)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/presence")
async def update_presence(presence: Presence):
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", presence.client_id, re.I):
        raise HTTPException(400, "Invalid client id")
    now = time.time()
    for client_id, seen in list(client_presence.items()):
        if now - seen > PRESENCE_TTL:
            client_presence.pop(client_id, None)
    if presence.visible:
        client_presence[presence.client_id] = now
    else:
        client_presence.pop(presence.client_id, None)
    return {"ok": True}


@app.get("/api/audit")
async def audit_log():
    return {"items": list(reversed(read_audit()))}


@app.get("/api/notifications")
async def notifications():
    return {"items": list(reversed(read_json(STATE / "notifications.json", list)))}


def read_pins():
    return [x for x in read_json(STATE / "pins.json", list) if isinstance(x, str)]


@app.get("/api/pins")
async def get_pins():
    return {"pins": read_pins()}


async def json_object(request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return body


@app.put("/api/pins")
async def put_pins(request: Request):
    body = await json_object(request)
    pins = body.get("pins")
    if not isinstance(pins, list) or len(pins) > 200 or not all(isinstance(x, str) and len(x) < 300 for x in pins):
        raise HTTPException(400, "Invalid pin list")
    pins = list(dict.fromkeys(pins))
    save_file(STATE / "pins.json", json.dumps(pins, ensure_ascii=False))
    return {"pins": pins}


def codex_limits():
    """Codex writes its rate limits into every session rollout; read the newest one (no config needed)."""
    files = sorted((Path.home() / ".codex/sessions").glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:6]
    for f in files:
        try:
            with f.open("rb") as fh:
                fh.seek(0, 2); fh.seek(max(0, fh.tell() - 200_000))
                lines = fh.read().decode("utf-8", "ignore").splitlines()
            found = parse_codex_limits(lines, f.stat().st_mtime)
        except OSError:
            continue
        if found:
            return found
    return None


UPLOAD_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/heic": ".heic", "image/heif": ".heic", "application/pdf": ".pdf", "text/plain": ".txt"}
UPLOAD_MAX = 15 * 1024 * 1024
VIEWABLE_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".txt", ".md", ".csv", ".json"}


def prune_uploads():
    """Files sent from the phone are kept a week."""
    try:
        old = list((STATE / "uploads").iterdir())
    except OSError:
        return
    for path in old:
        try:
            if time.time() - path.stat().st_mtime > 7 * 86400:
                path.unlink()
        except OSError:
            pass


@app.post("/api/sessions/{pane}/upload")
async def upload(pane: str, request: Request):
    """Save an image/PDF/text file sent from the phone and return its absolute path, ready to paste into a prompt."""
    must_exist(pane)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in UPLOAD_TYPES:
        raise HTTPException(415, "Unsupported file type")
    length = request.headers.get("content-length") or "0"
    if not (length.isascii() and length.isdigit()):
        raise HTTPException(400, "Invalid content length")
    too_large = HTTPException(413, "File too large (15 MB max)")
    if int(length) > UPLOAD_MAX:
        raise too_large
    data = bytearray()
    async for chunk in request.stream():  # the declared length may be missing, so stop at the limit while reading
        data += chunk
        if len(data) > UPLOAD_MAX:
            raise too_large
    if not data:
        raise too_large
    folder = STATE / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    prune_uploads()
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}{UPLOAD_TYPES[ctype]}"
    out = {"ok": True, "path": str(path)}
    if ctype == "image/jpeg":
        clean = strip_jpeg(data)  # the agent, and the model behind it, get the picture and not where it was taken
        data, out["meta"] = (clean, "removed") if clean else (data, "kept")
    elif ctype in ("image/heic", "image/heif"):
        out["meta"] = "kept"  # the phone redraws these before sending; one that arrives as it was could not be cleaned
    path.write_bytes(data)
    return {**out, "bytes": len(data)}


@app.get("/api/sessions/{pane}/file")
async def session_file(pane: str, path: str):
    """Serve a viewable document only from this pane's workspace or Ulak uploads."""
    item = must_exist(pane)
    folder = raw_agents.get(pane, {}).get("cwd") or item.get("cwd")
    try:
        cwd = Path(folder).expanduser().resolve() if folder else None  # never fall back to the folder Ulak runs in
        upload_dir = (STATE / "uploads").resolve()
        requested = Path(path).expanduser()
        candidate = (requested if requested.is_absolute() else (cwd or upload_dir) / requested).resolve()
        if candidate.suffix.lower() not in VIEWABLE_SUFFIXES:
            raise HTTPException(415, "Unsupported document type")
        inside = candidate.is_relative_to(upload_dir) or bool(
            cwd and candidate.is_relative_to(cwd) and not any(part.startswith(".") for part in candidate.relative_to(cwd).parts))
        found = inside and candidate.is_file()
    except (OSError, ValueError, RuntimeError):  # a NUL byte, a name too long, an unknown ~user, a symlink loop
        found = False
    if not found:
        raise HTTPException(404, "Document not found in this workspace")
    media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if media_type in {"text/markdown", "application/json"}:
        media_type = "text/plain"
    response = FileResponse(candidate, media_type=media_type, filename=candidate.name, content_disposition_type="inline")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/limits")
async def limits():
    """Usage limits from the files named in config `limit_files`, normalised to {providers: [...]}."""
    out = []
    for key, path in CONFIG.get("limit_files", {}).items():
        path = Path(path).expanduser()
        try:
            raw, mtime = path.read_text(), path.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue
        provider = parse_limits(key, raw, mtime)
        if provider:
            out.append(provider)
    if not any(p["id"] == "codex" for p in out):
        codex = codex_limits()
        if codex:
            out.append(codex)
    order = {"claude": 0, "codex": 1}
    out.sort(key=lambda p: order.get(p["id"], 2))  # Claude, Codex, then the rest (agy last)
    return {"providers": out, "now": time.time()}


@app.get("/api/push/public")
async def push_public():
    try:
        return {"key": (ROOT / ".secrets/vapid_public.txt").read_text().strip()}
    except OSError:
        return {"key": ""}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    sub = clean_subscription(await json_object(request))
    if not sub:
        raise HTTPException(400, "Not a push subscription")
    subs = [s for s in read_subs() if s.get("endpoint") != sub["endpoint"]][-(MAX_SUBS - 1):]
    subs.append(sub)
    write_subs(subs)
    return {"ok": True, "count": len(subs)}


@app.post("/api/push/lang")
async def push_lang(request: Request):
    body = await json_object(request)
    lang = "tr" if body.get("lang") == "tr" else "en"
    subs = read_subs()
    for s in subs:
        if s.get("endpoint") == body.get("endpoint"):
            s["lang"] = lang
    write_subs(subs)
    return {"ok": True}


@app.post("/api/push/known")
async def push_known(request: Request):
    body = await json_object(request)
    sub = next((s for s in read_subs() if s.get("endpoint") == body.get("endpoint")), None)
    return {"known": sub is not None, **{k: (sub or {}).get(k) for k in ("ok_at", "fail_at", "fail_code")}}


TEST_PUSH = {"en": "Test notification: it reaches this phone", "tr": "Deneme bildirimi: bu telefona ulaşıyor"}


@app.post("/api/push/test")
async def push_test(request: Request):
    """Send one message to the asking phone only, so its owner can see that notifications still arrive."""
    body = await json_object(request)
    sub = next((s for s in read_subs() if s.get("endpoint") == body.get("endpoint")), None)
    if not sub:
        raise HTTPException(404, "This device is not subscribed")
    code = await deliver(sub, {"test": True, "title": "Ulak", "body": TEST_PUSH.get(sub.get("lang"), TEST_PUSH["en"]), "tag": "ulak-test", "url": "/"})
    mark_deliveries({sub["endpoint"]: code})
    return {"ok": code == 201, "code": code}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8765, timeout_graceful_shutdown=2)
