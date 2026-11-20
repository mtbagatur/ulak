import asyncio
import getpass
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from chat import claude_chat, claude_path, detect_model, preview_from_screen, screen_chat, SPINNER, strip_ansi
from core import NotificationGate, bucket, build_push, is_destructive, parse_approval, parse_prompt, project_name, status_rank
from herdr_client import HerdrClient, HerdrError

ROOT = Path(__file__).parent
WEB = ROOT / "web"
STATE = ROOT / ".state"


def load_config():
    """Optional local config (gitignored): .secrets/config.json. Keys: rules_file, quick_targets, quick_label, limit_files."""
    try:
        return json.loads((ROOT / ".secrets/config.json").read_text())
    except (OSError, ValueError):
        return {}


CONFIG = load_config()
RULES = CONFIG.get("rules_file") or None
QUICK_TARGETS = {str(x).lower() for x in CONFIG.get("quick_targets", [])}
QUICK_LABEL = CONFIG.get("quick_label", "")
sessions = {}       # pane -> card (what the phone shows)
raw_agents = {}     # pane -> last agent record from herdr
since = {}          # pane -> {"status", "ts"}
pending_confirm = {}
subscribers = set()
pane_ids = set()
read_cache = {}     # pane -> (time, screen text)
pushed = {}         # pane -> last pushed prompt fingerprint
gate = NotificationGate()
client = HerdrClient()
allowed_login = os.environ.get("ULAK_ALLOWED_LOGIN", "")

TOOLS = {
    "claude": {"cmd": "claude", "models": {"": "", "sonnet": " --model sonnet", "opus": " --model opus", "haiku": " --model haiku"}},
    "codex": {"cmd": "codex", "models": {"": "", "gpt-6-luna": " -m gpt-6-luna"}},
    "agy": {"cmd": "agy", "models": {"": ""}},
}
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


# --- helpers -------------------------------------------------------------

def load_since():
    try:
        since.update(json.loads((STATE / "since.json").read_text()))
    except (OSError, ValueError):
        pass


def save_since():
    try:
        STATE.mkdir(exist_ok=True)
        (STATE / "since.json").write_text(json.dumps(since))
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


async def read_screen(pane, status, max_age=3.0, force=False):
    """Visible screen for a blocked pane, recent output otherwise (cached for a moment)."""
    hit = read_cache.get(pane)
    if hit and not force and time.time() - hit[0] < max_age and hit[2] == status:
        return hit[1]
    if status == "blocked":
        params = {"target": pane, "source": "visible"}
    else:
        params = {"target": pane, "source": "recent_unwrapped", "lines": 120}
    try:
        result = await client.call("agent.read", params)
    except HerdrError:
        result = await client.call("agent.read", {"target": pane, "source": "visible"})
    text = result.get("read", {}).get("text", "")
    read_cache[pane] = (time.time(), text, status)
    return text


def pending_from_read(text):
    parsed = parse_approval(text)
    if not parsed:
        return None
    parsed["destructive"] = is_destructive(parsed["command"], RULES)
    parsed["reason"] = parsed["question"]
    return parsed


async def fresh_pending(pane):
    # A blocked pane only exposes its visible screen (no scrollback).
    output = await client.call("agent.read", {"target": pane, "source": "visible"})
    return pending_from_read(output.get("read", {}).get("text", ""))


async def fresh_prompt(pane, agent):
    output = await client.call("agent.read", {"target": pane, "source": "visible"})
    p = parse_prompt(output.get("read", {}).get("text", ""), agent)
    if p:
        p["destructive"] = bool(p["kind"] == "approval" and is_destructive(p["details"], RULES))
    return p


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


async def make_card(agent, force=False):
    pane = agent["pane_id"]
    status = agent.get("agent_status") or agent.get("status") or "unknown"
    ts = touch_since(pane, status)
    project = project_name(agent)
    card = {
        "pane_id": pane, "agent": agent.get("agent") or "terminal", "status": status,
        "bucket": bucket(status, ts, time.time()), "since": ts or 0, "project": project,
        "title": subtitle(agent, project), "workspace_label": sessions.get(pane, {}).get("workspace_label", ""),
        "workspace_id": agent.get("workspace_id"), "cwd": display_cwd(agent.get("cwd")), "name": agent.get("name", ""),
        "model": sessions.get(pane, {}).get("model", ""), "preview": sessions.get(pane, {}).get("preview", ""), "pending": None,
        "quick": False,
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
                msgs, model = claude_chat(path, max_messages=30, tail_bytes=250_000)
                texts = [m["text"] for m in msgs if m["role"] == "assistant"]
                tools = [m for m in msgs if m["role"] == "tool"]
                if texts:
                    preview = re.sub(r"\s+", " ", re.sub(r"[*_`#>]+", "", texts[-1])).strip()[:140]
                elif tools:
                    preview = f"{tools[-1]['tool']}: {tools[-1]['text']}"[:140]
                card["transcript"] = True
                card["model"] = card["model"] or model
            except OSError:
                pass
    card["preview"] = preview or ("" if card.get("transcript") else preview_from_screen(screen)) or card["preview"]
    if status == "blocked":
        p = parse_prompt(screen, card["agent"])
        if p:
            card["pending"] = {"kind": p["kind"], "question": p["question"], "details": "\n".join(p["details"].splitlines()[-4:]),
                               "destructive": p["kind"] == "approval" and is_destructive(p["details"], RULES),
                               "fingerprint": p["fingerprint"], "cut": p.get("cut", False),
                               "roles": [o["role"] for o in p["options"]]}
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
    sem = asyncio.Semaphore(6)

    async def one(pane):
        async with sem:
            return await refresh_pane(pane, force=False)
    await asyncio.gather(*(one(p) for p in raw_agents), return_exceptions=True)
    pane_ids.update(panes)
    return sorted(sessions.values(), key=status_rank)


async def initialize():
    load_since()
    try:
        await load_all()
        await resubscribe()
    except Exception as exc:
        print(f"Herdr initialization failed: {exc}", flush=True)
    asyncio.create_task(event_loop())


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


async def handle_event(msg):
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
        pending_confirm.pop(pane, None)
        pane_ids.discard(pane)
        await resubscribe()
        await broadcast({"type": "removed", "pane": pane})
    elif event == "pane_agent_status_changed" and pane:
        agent = raw_agents.setdefault(pane, {"pane_id": pane})
        agent.update({k: v for k, v in data.items() if k not in {"type", "pane_id"}})
        agent["agent_status"] = data.get("agent_status", "unknown")
        card = await refresh_pane(pane)
        if card:
            await broadcast({"type": "session", "session": card})
            if card["status"] in ("blocked", "done"):
                asyncio.create_task(push_after_grace(pane, card["status"]))


async def push_after_grace(pane, status):
    await asyncio.sleep(10)
    item = sessions.get(pane)
    if not item or item.get("status") != status or not gate.allow(pane, status, time.time()):
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
    await send_push(build_push(item, prompt, destructive))


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
    path = ROOT / ".secrets/subscriptions.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("".join(json.dumps(s) + "\n" for s in subs))
    path.chmod(0o600)


def log_notification(payload):
    path = STATE / "notifications.json"
    try:
        STATE.mkdir(exist_ok=True)
        try:
            items = json.loads(path.read_text())
        except (OSError, ValueError):
            items = []
        items.append({"ts": time.time(), "pane": payload.get("pane"), "status": payload.get("status"),
                      "title": payload.get("title"), "body": payload.get("body")})
        path.write_text(json.dumps(items[-100:], ensure_ascii=False))
    except OSError:
        pass


async def send_push(payload):
    log_notification(payload)
    try:
        from pywebpush import WebPushException, webpush
        key = ROOT / ".secrets/vapid_private.pem"
        subs = read_subs()
        if not key.exists() or not subs:
            return
        topic = re.sub(r"[^A-Za-z0-9_-]", "-", payload.get("tag", "ulak"))[:32]
        dead = []
        for sub in subs:
            try:
                await asyncio.to_thread(webpush, sub, json.dumps(payload), str(key), {"sub": os.environ.get("ULAK_VAPID_SUBJECT", "mailto:local@localhost")},
                                        ttl=3600, headers={"Urgency": "high", "Topic": topic})
            except WebPushException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (404, 410):
                    dead.append(sub)
                print(f"Push delivery failed ({code}): {exc}", flush=True)
        if dead:
            write_subs([s for s in subs if s not in dead])
        print(f"Push sent: {payload.get('pane')} {payload.get('status')} {payload.get('body')[:60]!r}", flush=True)
    except Exception as exc:
        print(f"Push delivery failed: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app):
    await initialize()
    yield
    await client.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.middleware("http")
async def require_identity(request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in ("/", "/manifest.webmanifest", "/sw.js", "/api/push/public"):
        response = await call_next(request)
    else:
        login = request.headers.get("tailscale-user-login")
        if not allowed_login or not login or not secrets.compare_digest(login, allowed_login):
            return JSONResponse({"detail": "Tailscale identity required"}, status_code=403)
        response = await call_next(request)
    if path.startswith("/static") or path in ("/", "/sw.js", "/manifest.webmanifest"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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
        raise HTTPException(404, "Oturum kapalı ya da bilinmiyor")
    return sessions.get(pane) or {"pane_id": pane, "status": "unknown", "agent": "terminal"}


def next_blocked(exclude=None):
    for s in sorted(sessions.values(), key=status_rank):
        if s["status"] == "blocked" and s["pane_id"] != exclude:
            return s["pane_id"]
    return None


@app.get("/api/sessions")
async def list_sessions():
    try:
        return await load_all()
    except Exception as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/sessions/{pane}/chat")
async def chat(pane: str, limit: int = 60):
    item = must_exist(pane)
    status = item.get("status", "unknown")
    screen = await read_screen(pane, status, max_age=1.0)
    prompt = None
    if status == "blocked":
        prompt = parse_prompt(screen, item.get("agent", "claude"))
        if prompt:
            prompt["destructive"] = bool(prompt["kind"] == "approval" and is_destructive(prompt["details"], RULES))
    agent = raw_agents.get(pane, {})
    sid = (agent.get("agent_session") or {}).get("value")
    messages, source = None, "screen"
    if item.get("agent") == "claude" and sid:
        path = claude_path(agent.get("cwd"), sid)
        if path:
            try:
                messages, _ = claude_chat(path, max_messages=max(10, min(limit, 200)))
                source = "transcript"
            except OSError:
                messages = None
    if messages is None:
        messages = screen_chat(screen, max(10, min(limit, 200)))
    hint = next((l.strip() for l in reversed(strip_ansi(screen).splitlines()[-12:]) if SPINNER.match(l)), "") if status == "working" else ""
    return {"messages": messages, "source": source, "prompt": prompt, "status": status, "hint": hint, "session": item}


@app.post("/api/sessions/{pane}/prompt")
async def prompt(pane: str, action: Action):
    must_exist(pane)
    if not action.text or not action.text.strip():
        raise HTTPException(400, "Mesaj boş")
    return await client.call("agent.prompt", {"target": pane, "text": action.text})


@app.get("/api/config")
async def config():
    return {"quick_label": QUICK_LABEL if QUICK_TARGETS else ""}


@app.post("/api/quick")
async def to_quick(action: Action):
    target = next((s for s in sessions.values() if s.get("quick")), None)
    if not target:
        raise HTTPException(404, "Hedef oturum bulunamadı")
    if not action.text or not action.text.strip():
        raise HTTPException(400, "Mesaj boş")
    await client.call("agent.prompt", {"target": target["pane_id"], "text": action.text})
    return {"ok": True, "pane": target["pane_id"]}


@app.post("/api/sessions/{pane}/approve")
async def approve(pane: str, action: Action):
    item = sessions.get(pane)
    if not item or pane not in pane_ids:
        raise HTTPException(404, "Oturum kapalı ya da bilinmiyor")
    if item.get("status") != "blocked":
        raise HTTPException(409, "Oturum artık onay beklemiyor")
    if action.decision not in ("approve", "deny", "always"):
        raise HTTPException(400, "decision must be approve, deny, or always")
    # Always decide on the screen as it is now, never on a cached read.
    try:
        pending = await fresh_pending(pane)
    except Exception:
        pending = None
    if not pending:
        raise HTTPException(409, "Ekranda tanınan bir onay yok; terminali kullan")
    if not action.fingerprint or not secrets.compare_digest(action.fingerprint, pending["fingerprint"]):
        raise HTTPException(409, "Onay ekranı değişti; yenile")
    if action.decision not in pending["keys"]:
        raise HTTPException(409, "Bu istemde böyle bir seçenek yok")
    if pending.get("cut") and item.get("agent") == "claude" and action.decision != "deny":
        raise HTTPException(409, "Komutun tamamı görünmüyor; yalnız reddedilebilir")
    if action.decision == "always" and pending["destructive"]:
        raise HTTPException(400, "Yıkıcı komutta oturum boyu izin kapalı")
    if pending["destructive"] and action.decision != "deny":
        confirmation = pending_confirm.get(pane)
        valid = bool(confirmation and confirmation["expires"] > time.time() and confirmation["decision"] == action.decision and confirmation["fingerprint"] == pending["fingerprint"] and secrets.compare_digest(action.confirm_token or "", confirmation["token"]))
        if not valid:
            token = secrets.token_urlsafe(24)
            pending_confirm[pane] = {"token": token, "decision": action.decision, "fingerprint": pending["fingerprint"], "expires": time.time() + 120}
            return {"confirm_required": True, "confirm_token": token, "command": pending["command"]}
        pending_confirm.pop(pane, None)
    await client.call("agent.send_keys", {"target": pane, "keys": [pending["keys"][action.decision]]})
    return {"ok": True, "next": next_blocked(pane)}


@app.post("/api/sessions/{pane}/choose")
async def choose(pane: str, action: Action):
    item = sessions.get(pane)
    if not item or pane not in pane_ids:
        raise HTTPException(404, "Oturum kapalı ya da bilinmiyor")
    if item.get("status") != "blocked":
        raise HTTPException(409, "Oturum artık bir şey beklemiyor")
    p = await fresh_prompt(pane, item.get("agent", "claude"))
    if not p or p["kind"] in ("approval", "dismiss"):
        raise HTTPException(409, "Ekranda seçilecek bir menü yok")
    if not action.fingerprint or not secrets.compare_digest(action.fingerprint, p["fingerprint"]):
        raise HTTPException(409, "Ekran değişti; yenile")
    option = next((o for o in p["options"] if o["n"] == action.n), None)
    if not option:
        raise HTTPException(409, "Böyle bir seçenek yok")
    await client.call("agent.send_keys", {"target": pane, "keys": option["keys"]})
    return {"ok": True, "next": next_blocked(pane)}


@app.post("/api/sessions/{pane}/keys")
async def send_keys(pane: str, action: Action):
    item = must_exist(pane)
    keys = [ALLOWED_KEYS.get(str(k).lower()) for k in (action.keys or [])]
    if not keys or len(keys) > 4 or None in keys:
        raise HTTPException(400, "Geçersiz tuş")
    if item.get("status") == "blocked":
        pending = await fresh_pending(pane)
        if pending and pending["destructive"]:
            danger = {"Enter", pending["keys"].get("approve"), pending["keys"].get("always"), "y"}
            if any(k in danger for k in keys):
                raise HTTPException(409, "Yıkıcı komut: onay kartından iki adımla onayla")
    await client.call("agent.send_keys", {"target": pane, "keys": keys})
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
    for base in (home, home / "projeler"):
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
            "tools": {k: [m for m in v["models"] if m] for k, v in TOOLS.items()}}


@app.post("/api/sessions")
async def start_session(action: Action):
    tool = TOOLS.get(action.tool or "")
    if not tool or (action.model or "") not in tool["models"]:
        raise HTTPException(400, "Araç ya da model geçersiz")
    cwd = os.path.realpath(os.path.expanduser(action.cwd or ""))
    home = os.path.realpath(str(Path.home()))
    if not (cwd == home or cwd.startswith(home + "/")) or not os.path.isdir(cwd) or any(part.startswith(".") for part in Path(cwd[len(home):]).parts):
        raise HTTPException(400, "Klasör geçersiz")
    label = re.sub(r"[^A-Za-z0-9_-]", "", f"{Path(cwd).name}-{action.tool}")[:40] or "oturum"
    created = await client.call("workspace.create", {"cwd": cwd, "label": label, "focus": False})
    pane = created["root_pane"]["pane_id"]
    command = tool["cmd"] + tool["models"][action.model or ""]
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


@app.get("/api/notifications")
async def notifications():
    try:
        items = json.loads((STATE / "notifications.json").read_text())
    except (OSError, ValueError):
        items = []
    return {"items": list(reversed(items))}


def read_pins():
    try:
        items = json.loads((STATE / "pins.json").read_text())
        return [x for x in items if isinstance(x, str)]
    except (OSError, ValueError):
        return []


@app.get("/api/pins")
async def get_pins():
    return {"pins": read_pins()}


@app.put("/api/pins")
async def put_pins(request: Request):
    body = await request.json()
    pins = body.get("pins")
    if not isinstance(pins, list) or len(pins) > 200 or not all(isinstance(x, str) and len(x) < 300 for x in pins):
        raise HTTPException(400, "Geçersiz pin listesi")
    pins = list(dict.fromkeys(pins))
    STATE.mkdir(exist_ok=True)
    (STATE / "pins.json").write_text(json.dumps(pins, ensure_ascii=False))
    return {"pins": pins}


@app.get("/api/limits")
async def limits():
    result = {}
    for key, path in CONFIG.get("limit_files", {}).items():
        path = Path(path).expanduser()
        try:
            result[key] = json.loads(path.read_text()) if key == "claude" else path.read_text()
        except (OSError, json.JSONDecodeError):
            result[key] = None
    return result


@app.get("/api/push/public")
async def push_public():
    try:
        return {"key": (ROOT / ".secrets/vapid_public.txt").read_text().strip()}
    except OSError:
        return {"key": ""}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    body = await request.json()
    subs = [s for s in read_subs() if s.get("endpoint") != body.get("endpoint")]
    subs.append(body)
    write_subs(subs)
    return {"ok": True, "count": len(subs)}


@app.post("/api/push/known")
async def push_known(request: Request):
    body = await request.json()
    return {"known": any(s.get("endpoint") == body.get("endpoint") for s in read_subs())}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8765, timeout_graceful_shutdown=2)
