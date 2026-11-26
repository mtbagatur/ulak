"""Ulak demo: the real PWA over fake sessions. No Herdr, no auth, nothing leaves the machine.

    python tools/demo_server.py        # http://127.0.0.1:8766
"""
import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).resolve().parent.parent / "web"
NOW = time.time()


def card(pane, agent, status, bucket, project, ws, title, preview, ago, model="", pending=None):
    return {"pane_id": pane, "agent": agent, "status": status, "bucket": bucket, "since": NOW - ago, "project": project,
            "title": title, "workspace_label": ws, "workspace_id": ws, "cwd": f"~/projects/{project}", "name": "",
            "model": model, "preview": preview, "pending": pending, "quick": False, "transcript": agent == "claude"}


APPROVAL = {"kind": "approval", "question": "Do you want to proceed?", "destructive": False, "cut": False,
            "details": "pytest -q tests/test_billing.py", "fingerprint": "demo-a", "roles": ["approve", "always", "deny"]}
DESTRUCTIVE = {"kind": "approval", "question": "Do you want to proceed?", "destructive": True, "cut": False,
               "details": "git push origin release/2.4", "fingerprint": "demo-b", "roles": ["approve", "deny"]}

SESSIONS = [
    card("d1:p1", "claude", "blocked", "blocked", "billing-api", "work", "Fix flaky invoice test",
         "I found the race in the retry helper. Run the billing tests to confirm?", 90, "Sonnet", APPROVAL),
    card("d2:p1", "claude", "blocked", "blocked", "web-app", "work", "Ship 2.4",
         "Release notes are ready. Push the release branch?", 240, "Opus", DESTRUCTIVE),
    card("d3:p1", "codex", "working", "working", "infra", "ops", "Terraform plan cleanup",
         "Reading modules/network to find unused subnets…", 30, "gpt"),
    card("d4:p1", "claude", "working", "working", "docs", "work", "Rewrite the quickstart",
         "Editing docs/quickstart.md", 15, "Haiku"),
    card("d5:p1", "agy", "done", "done", "data-pipeline", "ops", "Backfill job",
         "Backfill finished: 1.2M rows, 0 errors.", 600, ""),
    card("d6:p1", "claude", "idle", "idle", "notes", "personal", "", "", 7200, "Sonnet"),
]

CHAT = [
    {"role": "user", "text": "The invoice test fails about one run in ten. Find out why."},
    {"role": "tool", "tool": "Grep", "text": "retry_with_backoff in src/billing", "result": "src/billing/retry.py:41: def retry_with_backoff(fn, tries=3):"},
    {"role": "assistant", "text": "Found it. `retry_with_backoff` sleeps with a **fixed** delay, so two workers retry in lockstep and collide on the same invoice row."},
    {"role": "tool", "tool": "Edit", "text": "src/billing/retry.py", "result": "+    delay = base * 2 ** attempt + random.uniform(0, base)"},
    {"role": "assistant", "text": "I added jitter to the backoff. Let me run the billing tests to confirm."},
]

import os
if os.environ.get("ULAK_DEMO_WORST"):  # break-ui: worst-case data (long names, unbreakable strings, huge command, RTL/CJK/emoji)
    LONG = "x" * 200
    BIG = "\n".join(f"find / -name '*.log' -exec rm -f {{}} \\; # step {i} " + "a" * 60 for i in range(60))
    SESSIONS[:] = [
        card("w1:p1", "claude", "blocked", "blocked", "a-very-long-project-name-that-keeps-going-and-going-and-going-forever", "workspace-with-a-long-label", LONG,
             "Unbreakable: " + LONG + " 日本語の長い文章がここに入ります 🙂🙂🙂 مرحبا بالعالم", 90, "Sonnet-4-5-with-an-absurdly-long-model-name",
             {**APPROVAL, "details": BIG, "question": "Do you want to proceed? " * 12}),
        card("w2:p1", "codex", "working", "working", "a", "", "", "", 5, ""),
        card("w3:p1", "agy", "done", "done", "同じ名前", "ws", "t" * 90, "y" * 400, 99999999),
        card("w4:p1", "claude", "idle", "idle", "same", "w1", "", "", 7200, ""),
        card("w5:p1", "claude", "idle", "idle", "same", "w2", "", "", 7200, ""),
    ]
    CHAT[:] = [{"role": "user", "text": "z" * 300 + "\n" * 5 + "end"},
               {"role": "assistant", "text": "```\n" + "long_line_" * 40 + "\n```\n" + LONG + "\nhttps://example.com/" + "p" * 200},
               {"role": "tool", "tool": "Bash", "text": BIG[:400], "result": BIG}]

app = FastAPI()
app.mount("/static", StaticFiles(directory=WEB), name="static")
pins = ["work|billing-api"]


@app.middleware("http")
async def fresh(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"  # same as the real app: a reload always gets the current build
    return response


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(WEB / "manifest.webmanifest")


@app.get("/sw.js")
async def sw():
    return FileResponse(WEB / "sw.js", media_type="application/javascript")


@app.get("/api/sessions")
async def sessions():
    return SESSIONS


@app.get("/api/sessions/{pane}/chat")
async def chat(pane: str):
    item = next(s for s in SESSIONS if s["pane_id"] == pane)
    prompt = None
    if item["pending"]:
        p = item["pending"]
        prompt = {**p, "options": [{"n": i + 1, "label": l, "role": r} for i, (l, r) in enumerate(
            [("Yes", "approve"), ("Yes, and don't ask again", "always"), ("No", "deny")] if p["roles"].count("always") else [("Yes", "approve"), ("No", "deny")])]}
    return {"messages": CHAT, "source": "transcript", "prompt": prompt, "status": item["status"], "hint": "", "session": item,
            "receipt": next((a for a in AUDIT if a["pane"] == pane and a["outcome"] == "sent"), None)}


@app.get("/api/sessions/{pane}/commands")
async def commands(pane: str):
    builtin = [{"name": n, "description": "", "source": "builtin"} for n in ("/clear", "/compact", "/help", "/model")]
    return {"commands": builtin + [
        {"name": "/review", "description": "Review the current diff before it ships", "source": "project"},
        {"name": "/release-notes", "description": "Draft release notes from merged changes", "source": "project"},
        {"name": "/git:sync", "description": "Rebase on main and rerun the tests", "source": "user"},
    ]}


@app.get("/api/sessions/{pane}/screen")
async def screen(pane: str):
    item = next((s for s in SESSIONS if s["pane_id"] == pane), None)
    command = (item or {}).get("pending") or {}
    lines = ["● I added jitter to the backoff. Let me run the billing tests to confirm.", ""]
    lines += ["Bash command", "", "  " + command["details"], "", "Do you want to proceed?", "❯ 1. Yes", "  2. No"] if command.get("details") else ["✻ Working… (esc to interrupt)"]
    return {"text": "\n".join(lines), "ts": time.time()}


@app.get("/api/config")
async def config():
    return {"quick_label": ""}


@app.get("/api/pins")
async def get_pins():
    return {"pins": pins}


@app.put("/api/pins")
async def put_pins(request: Request):
    pins[:] = (await request.json()).get("pins", [])
    return {"pins": pins}


AUDIT = []  # newest first, like the real /api/audit


@app.get("/api/audit")
async def audit():
    return {"items": AUDIT}


@app.get("/api/notifications")
async def notifications():
    return {"items": [{"ts": NOW - 90, "pane": "d1:p1", "status": "blocked", "title": "billing-api", "body": "Approval needed: pytest -q tests/test_billing.py"}]}


@app.get("/api/limits")
async def limits():
    n = time.time()
    return {"now": n, "providers": [
        {"id": "claude", "label": "Claude", "updated": n - 120, "items": [
            {"label": "5h", "used": 38, "resets_at": n + 2.4 * 3600}, {"label": "7d", "used": 71, "resets_at": n + 3.2 * 86400}]},
        {"id": "codex", "label": "Codex", "updated": n - 300, "items": [
            {"label": "5h", "used": 12, "resets_at": n + 4.1 * 3600}, {"label": "7d", "used": 91, "resets_at": n + 1.5 * 86400}]}]}


@app.get("/api/push/public")
async def push_public():
    return {"key": ""}


@app.get("/api/projects")
async def projects():
    return {"projects": [{"name": n, "path": f"/home/you/projects/{n}", "display": f"~/projects/{n}"} for n in ("billing-api", "web-app", "docs")],
            "tools": {"claude": ["sonnet", "opus"], "codex": [], "agy": []}}


@app.post("/api/sessions/{pane}/upload")
async def upload(pane: str):
    return {"ok": True, "path": "/home/you/.state/uploads/20261120-120000-abc123.png", "bytes": 1}


@app.post("/api/sessions/{pane}/approve")
async def approve(pane: str, request: Request):
    item = next((session for session in SESSIONS if session["pane_id"] == pane), None)
    if not item or not item.get("pending"):
        return JSONResponse({"detail": "No demo approval is pending"}, status_code=409)
    action = await request.json()
    pending = item["pending"]
    if action.get("fingerprint") != pending.get("fingerprint"):
        return JSONResponse({"detail": "The approval screen changed; refresh"}, status_code=409)
    if pending.get("destructive") and action.get("decision") == "approve":
        if action.get("confirm_token") != "demo-confirmation":
            return {"confirm_required": True, "confirm_token": "demo-confirmation", "command": pending["details"]}
    now = time.time()
    AUDIT.insert(0, {"id": f"demo-{len(AUDIT)}", "ts": now, "pane": pane, "project": item.get("project", ""), "decision": action.get("decision"),
                     "outcome": "sent", "reason": "", "command": pending["details"], "destructive": bool(pending.get("destructive")),
                     "login": "you@example.test", "delivered": now})
    item["pending"] = None
    item["status"] = "done"
    item["bucket"] = "done"
    item["since"] = time.time()
    return {"ok": True, "next": None}


@app.get("/api/events")
async def events():
    async def stream():
        yield "retry: 2000\nevent: ready\ndata: {}\n\n"
        while True:
            await asyncio.sleep(15)
            yield ": keepalive\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.api_route("/api/{rest:path}", methods=["POST", "PUT"])
async def swallow(rest: str):
    return {"ok": True, "pane": "d4:p1"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766)
