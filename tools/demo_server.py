"""Ulak demo: the real PWA over fake sessions. No Herdr, no auth, nothing leaves the machine.

    python tools/demo_server.py        # http://127.0.0.1:8766
"""
import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
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

app = FastAPI()
app.mount("/static", StaticFiles(directory=WEB), name="static")
pins = ["work|billing-api"]


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
    return {"messages": CHAT, "source": "transcript", "prompt": prompt, "status": item["status"], "hint": "", "session": item}


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


@app.get("/api/notifications")
async def notifications():
    return {"items": [{"ts": NOW - 90, "pane": "d1:p1", "status": "blocked", "title": "billing-api", "body": "Approval needed: pytest -q tests/test_billing.py"}]}


@app.get("/api/limits")
async def limits():
    return {}


@app.get("/api/push/public")
async def push_public():
    return {"key": ""}


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
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766)
