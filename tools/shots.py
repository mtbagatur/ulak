"""Retake the README screenshots and the demo GIF from the scripted demo.

    python tools/shots.py            # docs/screenshots/{en,tr}/*.png, docs/demo.gif and docs/demo-tr.gif

Needs Chrome or Chromium, the dev requirements, and ffmpeg for the GIF.
"""
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_web import APPROVE, open_chrome  # noqa: E402
from tools import demo_server as demo  # noqa: E402

DOCS = ROOT / "docs"
WIDTH, HEIGHT = 390, 780
FPS = 10
# What the fake agent says once the push is through, per language (tools/demo/demo.js).
PUSHED = {"en": "Pushed. CI is running", "tr": "Gönderildi."}
GIFS = {"en": "demo.gif", "tr": "demo-tr.gif"}
TAP = """(() => {
  const el = document.querySelector(%s), r = el.getBoundingClientRect(), dot = document.createElement('i');
  dot.style.cssText = `position:fixed;z-index:99;left:${r.left + r.width / 2 - 17}px;top:${r.top + r.height / 2 - 17}px;width:34px;height:34px;border-radius:50%%;`
    + 'background:rgba(255,255,255,.28);box-shadow:0 0 0 1.5px rgba(255,255,255,.6),0 0 0 3px rgba(0,0,0,.25);pointer-events:none;transition:opacity .35s .25s';
  document.body.append(dot);
  requestAnimationFrame(() => { dot.style.opacity = 0; });
  setTimeout(() => dot.remove(), 700);
})()"""


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Phone:
    def __init__(self, chrome, url):
        self.chrome, self.url, self.frames = chrome, url, None

    def start(self, lang, speed, scheme="dark", scale=2):
        c = self.chrome
        c.cdp("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT, deviceScaleFactor=scale, mobile=True)
        c.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}])
        c.cdp("Page.navigate", url=self.url)
        c.wait("document.querySelectorAll('.card').length === 6")
        c.js(f"localStorage.setItem('lang', {json.dumps(lang)})")
        c.cdp("Page.navigate", url=f"{self.url}?speed={speed}&{lang}{scheme}{scale}")
        c.wait("document.querySelectorAll('.card').length === 6")
        self.hold(0.6)

    def png(self):
        return base64.b64decode(self.chrome.cdp("Page.captureScreenshot", format="png")["data"])

    def hold(self, seconds):
        """Let time pass. While recording, every tenth of a second becomes a frame; headless Chrome only advances CSS animations when it draws."""
        end = time.monotonic() + seconds
        while True:
            tick = time.monotonic()
            frame = self.png()
            if self.frames is not None:
                (self.frames / f"{len(list(self.frames.iterdir())):05d}.png").write_bytes(frame)
            if tick + 1 / FPS >= end:
                break
            time.sleep(max(0, tick + 1 / FPS - time.monotonic()))

    def until(self, expression, timeout=30):
        end = time.monotonic() + timeout
        while not self.chrome.js(expression):
            assert time.monotonic() < end, expression
            self.hold(0.1)

    def tap(self, selector):
        self.until(f"document.querySelector({json.dumps(selector)}) !== null")
        if self.frames is not None:
            self.chrome.js(TAP % json.dumps(selector))
            self.hold(0.35)
        self.chrome.click(selector)

    def shot(self, path, toast=False):
        if not toast:
            self.until("document.querySelector('#toasts').children.length === 0")  # a passing toast covers the title
        self.hold(0.5)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.png())
        print(path.relative_to(ROOT))

    def open(self, pane):
        self.tap(f".card[data-key={json.dumps(pane)}] .card-open")
        self.until(f"location.hash === {json.dumps('#/s/' + pane)} && document.querySelector('#messages .msg') !== null")

    def home(self):
        self.tap("#back")
        self.until("!document.querySelector('#home').hidden")


def pushed(lang):
    return f"document.querySelector('#messages').textContent.includes({json.dumps(PUSHED[lang])})"


def stills(phone, lang):
    out = DOCS / "screenshots" / lang
    phone.start(lang, speed=1)
    phone.shot(out / "home.png")
    phone.open("d5:p1")
    phone.until("document.querySelectorAll('#messages .run').length >= 2")
    phone.chrome.js("(() => { const run = document.querySelectorAll('#messages .run')[1]; if (!run.classList.contains('open')) run.querySelector('button').click(); })()")
    phone.until("document.querySelectorAll('#messages .run')[1].classList.contains('open')")
    phone.hold(0.4)
    phone.chrome.js("document.querySelector('#messages .act.bad').scrollIntoView({block: 'start'})")
    phone.shot(out / "reading.png")
    phone.tap("#sMeta")
    phone.until("document.querySelector('#sheet [role=switch]') !== null")
    phone.shot(out / "handoff.png")
    phone.tap("#sheet [data-close]")
    phone.home()
    phone.open("d1:p1")
    phone.shot(out / "approval.png")
    phone.tap("#sMenu")
    phone.tap("#sheet [data-d]")
    phone.until("document.querySelector('#sheet .diff') !== null")
    phone.shot(out / "changes.png")
    phone.tap("#sheet [data-close]")
    phone.tap(APPROVE)
    phone.until("location.hash === '#/s/d2:p1' && document.querySelector('#promptZone .two-seals') !== null")
    phone.shot(out / "two-seals.png")
    phone.tap(APPROVE)
    phone.until("document.querySelector('#sheet [data-y]') !== null")
    phone.shot(out / "second-seal.png")
    phone.tap("#sheet [data-y]")
    phone.until("document.querySelector('#receipt.ok') !== null && document.querySelector('#messages .hint') !== null")
    phone.shot(out / "delivered.png")
    phone.until(pushed(lang))
    phone.home()
    phone.until("document.querySelector('.card[data-key=\"d3:p1\"] [data-act=approve]') !== null")
    phone.open("d3:p1")
    phone.tap(APPROVE)
    phone.until("document.querySelector('#toasts .err') !== null && document.querySelector('#promptZone .two-seals') !== null")
    phone.shot(out / "refused.png", toast=True)
    phone.until("document.querySelector('#toasts .err') === null")
    phone.home()
    phone.tap("#history")
    phone.tap("#sheet [data-tab=decisions]")
    phone.shot(out / "decisions.png")
    phone.start(lang, speed=1, scheme="light")
    phone.shot(out / "light.png")


def gif(phone, work, lang):
    frames = work / f"frames-{lang}"
    frames.mkdir()
    phone.start(lang, speed=1.6, scale=1)
    phone.frames = frames
    phone.hold(1.6)
    phone.open("d1:p1")
    phone.hold(1.8)
    phone.tap(APPROVE)
    phone.until("location.hash === '#/s/d2:p1' && document.querySelector('#promptZone .two-seals') !== null")
    phone.hold(2.2)
    phone.tap(APPROVE)
    phone.until("document.querySelector('#sheet [data-y]') !== null")
    phone.hold(1.8)
    phone.tap("#sheet [data-y]")
    phone.until(pushed(lang))
    phone.hold(1.6)
    phone.home()
    phone.until("document.querySelector('.card[data-key=\"d3:p1\"] [data-act=approve]') !== null")
    phone.hold(1.2)
    phone.open("d3:p1")
    phone.hold(1.6)
    phone.tap(APPROVE)
    phone.until("document.querySelector('#toasts .err') !== null && document.querySelector('#promptZone .two-seals') !== null")
    phone.hold(3.2)
    phone.frames = None
    palette = "split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames / "%05d.png"), "-vf", palette, str(DOCS / GIFS[lang])], check=True)
    print(f"docs/{GIFS[lang]}  {len(list(frames.iterdir())) / FPS:.0f}s  {(DOCS / GIFS[lang]).stat().st_size // 1024} KB")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        demo.build(work / "site")
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(work / "site")))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with open_chrome(work) as chrome:
                phone = Phone(chrome, f"http://127.0.0.1:{server.server_port}/")
                for lang in ("en", "tr"):
                    stills(phone, lang)
                if shutil.which("ffmpeg"):
                    for lang in GIFS:
                        gif(phone, work, lang)
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
