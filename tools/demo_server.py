"""Ulak demo: the real PWA over a scripted, fake machine (tools/demo/demo.js). No Herdr, no auth, nothing leaves the tab.

    python tools/demo_server.py               # http://127.0.0.1:8766  (?speed=4 hurries the agents, ?worst loads worst-case data)
    python tools/demo_server.py --build DIR   # a static copy for any web host; works under a subpath
"""
import re
import shutil
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "web"
SHIM = ROOT / "demo" / "demo.js"


def index_html():
    """The app's own page with relative paths, no install manifest, and the fake machine loaded before the app."""
    html = (WEB / "index.html").read_text().replace('"/static/', '"static/')
    html = re.sub(r'\s*<link rel="manifest"[^>]*>', "", html)
    html = html.replace('<script src="static/i18n.js">', '<script src="static/demo.js"></script>\n<script src="static/i18n.js">', 1)
    assert "static/demo.js" in html and not re.search(r'(?:href|src)="/', html), "index.html changed shape; update the demo build"
    return html


def build(out):
    out = Path(out)
    static = out / "static"
    static.mkdir(parents=True, exist_ok=True)
    for item in WEB.iterdir():
        if item.name not in ("index.html", "sw.js", "manifest.webmanifest"):
            shutil.copy2(item, static / item.name)
    shutil.copy2(SHIM, static / "demo.js")
    (out / "index.html").write_text(index_html())
    return out


app = FastAPI()


@app.middleware("http")
async def fresh(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"  # same as the real app: a reload always gets the current build
    return response


@app.get("/")
async def index():
    return HTMLResponse(index_html())


@app.get("/static/demo.js")
async def shim():
    return FileResponse(SHIM, media_type="application/javascript")


app.mount("/static", StaticFiles(directory=WEB), name="static")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--build"] and len(sys.argv) == 3:
        print(build(sys.argv[2]))
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8766)
