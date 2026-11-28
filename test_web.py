"""Chrome smoke tests against the static PWA and a loopback-only fake API."""

import re
import json
import struct
import zlib
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote
from pathlib import Path

import pytest
from core import CSP


WEB = Path(__file__).with_name("web")
PANE = "fake:p1"
APPROVE = '#promptZone [data-d="approve"]'
APPROVAL = {
    "kind": "approval",
    "details": "pytest -q",
    "hook_note": "A hook requested confirmation.",
    "fingerprint": "first-approval",
    "destructive": False,
    "cut": False,
    "roles": ["approve", "deny"],
    "options": [{"role": "approve"}, {"role": "deny"}],
}
DESTRUCTIVE = {**APPROVAL, "fingerprint": "destructive-approval", "details": "git push origin example", "destructive": True}
OTHER_APPROVAL = {**APPROVAL, "fingerprint": "second-approval", "details": "pytest -q test_web.py"}
CHOICE = {"kind": "choice", "question": "Which database?", "details": "", "fingerprint": "first-choice", "destructive": False,
          "cut": False, "hidden": [], "roles": ["choice"] * 3, "multi": False, "header": "",
          "options": [{"n": n, "label": label} for n, label in enumerate(["SQLite", "Postgres", "MySQL"], 1)]}


def _png(width, height):
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = (b"\0" + b"\x60\x40\x80" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


PNG = _png(40, 400)  # taller than the box it is shown in


def card(status, **fields):
    return {
        "pane_id": PANE, "project": "fixture", "agent": "claude", "model": "Sonnet",
        "workspace_label": "test-workspace", "status": status, "bucket": status,
        "since": time.time(), "pending": None, **fields,
    }


class FakeAPI:
    def __init__(self):
        self.cards = [card("blocked", pending=APPROVAL)]
        self.limits = []
        self.push_tests = []
        self.uploads = []
        self.files = []
        self.push_test_reply = {"ok": True, "code": 201}
        self.messages = [{"role": "assistant", "text": "Example answer"}]
        self.source = "transcript"
        self.approval = APPROVAL
        self.events = []
        self.requests = []
        self.replies = []
        self.receipt = None
        self.audit = []
        self.commands = None
        self.screen = "idle"
        self.config = {"quick_label": "", "approval_timeout_minutes": 0}
        self.changes = {"repo": False, "branch": "", "files": [], "more_files": 0, "diff": "", "truncated": False}
        self.presence = []
        self.fail_prompt = False
        self.prompts = []
        self.pins = []
        self.notifications, self.handoff_calls, self.handoff_fail = [], [], False
        self.moved = None
        self.start, self.conversation, self.conversations, self.pages, self.anchors = None, "one", [], {}, []
        self.meters, self.clock_shift = {}, 0
        self.search, self.search_status, self.searches, self.history = {"hits": [], "partial": False}, 200, [], []
        self.files, self.missing, self.file_gate = [], set(), threading.Event()
        self.file_gate.set()
        self.respond = threading.Event()
        self.respond.set()
        self.ended = threading.Event()
        self.stream_generation = 0
        self.ready = threading.Event()
        self.fail_list = False
        self.fail_chat = False
        self.list_failed = threading.Event()
        self.chat_failed = threading.Event()
        self.list_started = threading.Event()
        self.list_respond = threading.Event()
        self.list_respond.set()

    def publish_prompt(self, approval):
        self.publish_session("blocked", approval)

    def publish_session(self, status, approval=None):
        self.approval = approval
        self.cards = [card(status, pending=approval)]
        self.events.append({"type": "session", "session": self.cards[0]})


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, api, *args, **kwargs):
        self.api = api
        super().__init__(*args, directory=WEB, **kwargs)

    def log_message(self, *_args):
        pass

    def end_headers(self):
        if not self.path.startswith("/api/"):  # every page test runs under the policy the real server sends
            self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/api/events":
            self.stream()
        elif self.path == "/api/sessions":
            if self.api.fail_list:
                self.json({"detail": "Fixture snapshot unavailable"}, 503)
                self.api.list_failed.set()
                return
            rows = self.api.cards
            self.api.list_started.set()
            assert self.api.list_respond.wait(8), "test did not release the held snapshot"
            self.json(rows)
        elif self.path.split("?")[0] == "/api/sessions/fake%3Ap1/chat":
            if self.api.fail_chat:
                self.json({"detail": "Fixture chat unavailable"}, 503)
                self.api.chat_failed.set()
                return
            item = self.api.cards[0]
            anchor = self.path.partition("?from=")[2]
            self.api.anchors.append(anchor)
            self.json({
                "messages": self.api.messages, "source": self.api.source, "prompt": self.api.approval,
                "status": item["status"], "session": item, "receipt": self.api.receipt,
                "start": self.api.moved or (int(anchor) if anchor else self.api.start), "conversation": self.api.conversation,
                "meters": self.api.meters, "now": time.time() + self.api.clock_shift,
            })
        elif self.path.startswith("/api/sessions/fake%3Ap1/search?q="):
            self.api.searches.append(unquote(self.path.partition("?q=")[2]))
            self.json(self.api.search if self.api.search_status == 200 else {"detail": "No conversation to search"}, self.api.search_status)
        elif self.path.startswith("/api/sessions/fake%3Ap1/history?"):
            self.api.history.append(self.path.partition("?")[2])
            page = self.api.pages.get(self.path.partition("?")[2])
            self.json(page) if page else self.json({"detail": "No such conversation"}, 404)
        elif self.path.startswith("/api/sessions/fake%3Ap1/file?path="):
            path = unquote(self.path.partition("?path=")[2])
            self.api.files.append(path)
            assert self.api.file_gate.wait(8), "test did not release the held picture"
            if path in self.api.missing:
                self.json({"detail": "No such file"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        elif self.path == "/api/sessions/fake%3Ap1/conversations":
            self.json({"items": self.api.conversations})
        elif self.path == "/api/sessions/fake%3Ap1/commands" and self.api.commands is not None:
            self.json({"commands": self.api.commands})
        elif self.path == "/api/sessions/fake%3Ap1/screen":
            self.json({"text": self.api.screen, "ts": time.time()})
        elif self.path == "/api/sessions/fake%3Ap1/changes":
            self.json(self.api.changes)
        elif self.path == "/api/audit":
            self.json({"items": self.api.audit})
        elif self.path == "/api/notifications":
            self.json({"items": self.api.notifications})
        elif self.path == "/api/pins":
            self.json({"pins": self.api.pins})
        elif self.path == "/api/limits":
            self.json({"now": time.time(), "providers": self.api.limits})
        elif self.path == "/api/config":
            self.json(self.api.config)
        else:
            if self.path.startswith("/static/"):
                self.path = self.path.removeprefix("/static")
            super().do_GET()

    def stream(self):
        api = self.api
        generation = api.stream_generation
        cursor = len(api.events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(b"event: ready\ndata: {}\n\n")
            api.ready.set()
            while not api.ended.wait(0.05) and generation == api.stream_generation:
                for event in api.events[cursor:]:
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    cursor += 1
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handoff(self, change):
        """The card the real server answers with: the session as it now stands."""
        api = self.api
        api.handoff_calls.append((self.command, change))
        assert api.respond.wait(8), "test did not release the held handoff call"
        if api.handoff_fail:
            self.json({"detail": "Herdr is not reachable"}, 503)
            return
        was = api.cards[0].get("handoff") or {}
        now = {"on": change["on"], "phase": "armed", "due": None} if "on" in change else {**was, "phase": "held", "due": None}
        api.cards = [{**api.cards[0], "handoff": now}]
        self.json(api.cards[0])

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/sessions/fake%3Ap1/handoff":
            self.handoff(body)
        elif self.path == "/api/pins":
            self.api.pins = body.get("pins", [])
            self.json({"pins": self.api.pins})
        else:
            self.send_error(404)

    def do_POST(self):
        api = self.api
        if self.path == "/api/sessions/fake%3Ap1/upload":
            data = self.rfile.read(int(self.headers["Content-Length"]))
            kind = self.headers["Content-Type"]
            api.uploads.append((kind, data[:4]))
            api.files.append(data)
            self.json({"ok": True, "path": f"/tmp/up-{len(api.uploads)}", "bytes": len(data), **({"meta": "kept"} if kind == "image/heic" else {"meta": "removed"} if kind == "image/jpeg" else {})})
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/presence":
            api.presence.append(body)
            self.json({"ok": True})
            return
        if self.path == "/api/push/test":
            api.push_tests.append(body)
            self.json(api.push_test_reply)
            return
        if self.path == "/api/sessions/fake%3Ap1/handoff/cancel":
            self.handoff({})
            return
        if self.path == "/api/sessions/fake%3Ap1/prompt":
            api.prompts.append(body["text"])
            assert api.respond.wait(8), "test did not release the held message"
            self.json({"detail": "Herdr is down"}, 503) if api.fail_prompt else self.json({"ok": True})
            return
        if self.path == "/api/sessions/fake%3Ap1/choose":
            api.requests.append(body)
            assert api.respond.wait(8), "test did not release the held choice"
            if body["fingerprint"] != api.approval["fingerprint"]:
                self.json({"detail": "The screen changed; refresh"}, 409)
            else:
                api.replies.append(("choose", body["n"], body["fingerprint"]))
                self.json({"ok": True, "next": None})
            return
        if self.path != "/api/sessions/fake%3Ap1/approve":
            self.send_error(404)
            return
        api.requests.append(body)
        assert api.respond.wait(8), "test did not release the held approval"
        prompt = api.approval
        if body["fingerprint"] != prompt["fingerprint"]:
            self.json({"detail": "The approval screen changed; refresh"}, 409)
        elif prompt["destructive"] and body["decision"] == "approve" and body.get("confirm_token") != "test-confirmation":
            self.json({"confirm_required": True, "confirm_token": "test-confirmation", "command": prompt["details"]})
        else:
            api.replies.append(body)
            self.json({"ok": True, "next": None})


@contextmanager
def serve_fake_api():
    api = FakeAPI()
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, api)) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield api, f"http://127.0.0.1:{server.server_port}"
        finally:
            api.respond.set()
            api.list_respond.set()
            api.ended.set()
            server.shutdown()
            worker.join()


def wait_for(check, description, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.03)
    pytest.fail(f"Timed out waiting for {description}")


class Chrome:
    def __init__(self, ws):
        self.ws = ws
        self.request_id = 0

    def cdp(self, method, **params):
        self.request_id += 1
        self.ws.send(json.dumps({"id": self.request_id, "method": method, "params": params}))
        while True:
            response = json.loads(self.ws.recv(timeout=5))
            if response.get("id") == self.request_id:
                assert "error" not in response, response
                return response["result"]

    def js(self, expression):
        result = self.cdp("Runtime.evaluate", expression=expression, returnByValue=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def wait(self, expression):
        return wait_for(lambda: self.js(expression), expression)

    def click(self, selector):
        self.wait(f"document.querySelector({json.dumps(selector)}) !== null")
        self.js(f"document.querySelector({json.dumps(selector)}).click()")

    def key(self, key, code, virtual_key, modifiers=0):
        params = {"key": key, "code": code, "windowsVirtualKeyCode": virtual_key, "modifiers": modifiers}
        if key == "Enter":
            self.cdp("Input.dispatchKeyEvent", type="keyDown", text="\r", **params)
        else:
            self.cdp("Input.dispatchKeyEvent", type="rawKeyDown", **params)
        self.cdp("Input.dispatchKeyEvent", type="keyUp", **params)

    def resize(self, width, height):
        self.cdp("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=True)

    def offline(self, enabled):
        self.cdp("Network.emulateNetworkConditions", offline=enabled, latency=0, downloadThroughput=-1, uploadThroughput=-1)


@contextmanager
def open_chrome(profile):
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        pytest.skip("Chrome or Chromium is required for PWA smoke tests")
    websocket = pytest.importorskip("websockets.sync.client")
    with (profile / "chrome.log").open("w+") as log, subprocess.Popen([
        chrome, "--headless", "--no-first-run", "--disable-background-networking", "--disable-dev-shm-usage", "--no-proxy-server",
        "--lang=en-US", "--remote-debugging-port=0", f"--user-data-dir={profile / 'profile'}", "about:blank",
    ], stdout=log, stderr=log) as proc:
        port_file = profile / "profile" / "DevToolsActivePort"

        def port_ready():
            if proc.poll() is not None:
                log.seek(0)
                pytest.fail(f"Chrome exited during startup:\n{log.read()}")
            return port_file.exists() and port_file.stat().st_size > 0

        try:
            wait_for(port_ready, "Chrome's DevTools port", timeout=60)  # a cold CI machine takes a while the first time
            port = port_file.read_text().splitlines()[0]
            devtools = HTTPConnection("127.0.0.1", int(port), timeout=5)
            try:
                devtools.request("GET", "/json/list")
                target = next(t for t in json.load(devtools.getresponse()) if t["type"] == "page")
            finally:
                devtools.close()
            with websocket.connect(target["webSocketDebuggerUrl"], open_timeout=5, max_size=None) as ws:
                yield Chrome(ws)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()


@pytest.fixture
def api_fixture(tmp_path):
    with open_chrome(tmp_path) as chrome, serve_fake_api() as (api, url):
        yield chrome, api, url


def open_settings(chrome):
    if chrome.js("document.querySelector('#sheet #refresh') === null"):
        chrome.click("#settings")
        chrome.wait("document.querySelector('#sheet #refresh') !== null")


def open_first(chrome, url):
    chrome.cdp("Page.navigate", url=url)
    chrome.click(".card-open")
    chrome.wait("!document.querySelector('#session').hidden && document.querySelector('#messages .msg') !== null")


def test_inline_approval_ignores_second_keyboard_reply_while_request_is_pending(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('[data-act=approve]') !== null")
    api.respond.clear()
    chrome.js("document.querySelector('[data-act=approve]').focus()")
    chrome.key("Enter", "Enter", 13)
    chrome.wait("document.activeElement.getAttribute('aria-disabled') === 'true'")
    chrome.key("Enter", "Enter", 13)
    chrome.click("[data-act=deny]")
    api.respond.set()
    chrome.wait("document.querySelector('#toasts .ok')?.textContent === 'Reply sent'")
    assert chrome.js("document.activeElement.getAttribute('aria-disabled')") == "true"
    chrome.key("Enter", "Enter", 13)
    api.publish_prompt({**APPROVAL, "fingerprint": "follow-up"})
    chrome.wait("document.activeElement.matches('[data-act=approve]') && !document.activeElement.hasAttribute('aria-disabled')")
    assert api.requests == [{"decision": "approve", "fingerprint": APPROVAL["fingerprint"]}]


def test_inline_approval_displays_entire_wrapped_command_at_320_px(api_fixture):
    chrome, api, url = api_fixture
    command = "\n".join("echo " + character * 46 for character in "abc")
    api.publish_prompt({**APPROVAL, "details": command})
    chrome.resize(320, 640)
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card [data-act=approve]') !== null")
    assert chrome.js("document.querySelector('.card .c-cmd').textContent") == command
    assert chrome.js("""(() => {
        const command = document.querySelector('.card .c-cmd');
        return command.scrollHeight <= command.clientHeight && command.scrollWidth <= command.clientWidth;
    })()""")


def test_destructive_approval_shows_command_and_focuses_dialog_before_sending(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt(DESTRUCTIVE)
    open_first(chrome, url)
    chrome.wait(f"document.querySelector({json.dumps(APPROVE)}) !== null")
    assert not chrome.js("document.querySelector('[data-act=approve]') !== null")
    chrome.js(f"document.querySelector({json.dumps(APPROVE)}).focus()")
    chrome.key("Enter", "Enter", 13)
    chrome.wait("!document.querySelector('#sheet').hidden")
    assert chrome.js("document.querySelector('#sheet pre').textContent") == DESTRUCTIVE["details"]
    assert chrome.js("document.querySelector('#sheet').getAttribute('aria-labelledby')") == "sheetTitle"
    assert chrome.js("document.activeElement.matches('#sheet [data-x]')")
    assert not api.replies
    assert not chrome.js("document.querySelector('#toasts .ok') !== null")
    chrome.key("Tab", "Tab", 9, modifiers=8)
    assert chrome.js("document.activeElement.matches('#sheet [data-y]')")
    chrome.key("Tab", "Tab", 9)
    assert chrome.js("document.activeElement.matches('#sheet [data-x]')")
    chrome.key("Escape", "Escape", 27)
    chrome.js("document.querySelector('#sheet [data-y]')?.click()")
    chrome.wait("document.querySelector('#sheet').hidden")
    assert len(api.requests) == 1
    assert chrome.js(f"document.activeElement.matches({json.dumps(APPROVE)})")
    chrome.key("Enter", "Enter", 13)
    chrome.wait("!document.querySelector('#sheet').hidden")
    chrome.click("#sheet [data-y]")
    chrome.wait("document.querySelector('#toasts .ok')?.textContent === 'Reply sent'")
    assert api.replies == [{"decision": "approve", "fingerprint": DESTRUCTIVE["fingerprint"], "confirm_token": "test-confirmation"}]


def test_changed_prompt_cannot_be_approved_by_old_confirmation_sheet(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt(DESTRUCTIVE)
    open_first(chrome, url)
    chrome.click(APPROVE)
    chrome.wait("!document.querySelector('#sheet').hidden")
    api.publish_prompt(OTHER_APPROVAL)
    chrome.wait(f"document.querySelector('#promptZone pre')?.textContent === {json.dumps(OTHER_APPROVAL['details'])}")
    chrome.wait("document.querySelector('#sheet').hidden")
    assert chrome.js("document.activeElement.id") == "back"
    assert not api.replies
    assert len(api.requests) == 1 and api.requests[0]["fingerprint"] == DESTRUCTIVE["fingerprint"]


def test_unknown_after_reply_times_out_without_resending_or_leaving_chat(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#toasts .ok')?.textContent === 'Reply sent'")
    api.publish_session("unknown")
    chrome.wait("[...document.querySelectorAll('#toasts .err')].some(e => e.textContent.includes('could not verify a screen change'))")
    assert chrome.js("location.hash") == "#/s/fake:p1"
    assert len(api.replies) == len(api.requests) == 1


def test_approval_409_shows_error_and_refreshes_prompt(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url + "#/s/" + PANE)
    chrome.wait("document.querySelector('#promptZone pre')?.textContent === 'pytest -q'")
    api.approval = OTHER_APPROVAL  # The backend sees the change before it is delivered to the browser.
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#toasts .err')?.textContent === 'The approval screen changed; refresh'")
    chrome.wait(f"document.querySelector('#promptZone pre')?.textContent === {json.dumps(OTHER_APPROVAL['details'])}")
    assert not api.replies


@pytest.mark.parametrize("width", [320, 375, 1280])
def test_unknown_state_and_long_labels_stay_visible(api_fixture, width):
    chrome, api, url = api_fixture
    project = "project-" + "x" * 120
    workspace = "workspace-" + "y" * 120
    api.cards = [card("unknown", project=project, workspace_label=workspace), card("idle", pane_id="fake:p2", project=project)]
    chrome.resize(width, 640)
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.g-unknown .ws') !== null")
    assert chrome.js("innerWidth") == width
    assert chrome.js("document.querySelector('#summary').textContent") == "Unknown status: 1"
    assert chrome.js("document.querySelector('.g-idle .cards').hidden")
    assert chrome.js("document.querySelector('.g-unknown .ws').textContent") == workspace
    assert chrome.js("document.querySelector('.g-unknown .card-open').getAttribute('aria-label')") == f"Open {project}"
    assert chrome.js("document.documentElement.scrollWidth") == width
    assert chrome.js("document.querySelector('.g-unknown .c-top').scrollWidth <= document.querySelector('.g-unknown .c-top').clientWidth")
    chrome.js("document.querySelector('button[data-toggle]').focus()")
    chrome.key("Enter", "Enter", 13)
    assert chrome.js("document.activeElement.getAttribute('aria-expanded') === 'true' && !document.querySelector('#idleCards').hidden")
    chrome.key("Enter", "Enter", 13)
    assert chrome.js("document.activeElement.getAttribute('aria-expanded') === 'false' && document.querySelector('#idleCards').hidden")
    chrome.click("#settings")
    chrome.click("#sheet #lang")
    assert chrome.js("document.querySelector('#summary').textContent") == "Durumu bilinmeyen oturum: 1"


def test_long_choice_prompt_scrolls_without_hiding_composer(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt({
        "kind": "choice", "fingerprint": "long-choice", "question": "Choose a test path " * 18,
        "options": [{"n": n, "label": f"Path-{n}-" + "long" * 30} for n in range(1, 8)],
    })
    chrome.resize(320, 440)
    open_first(chrome, url)
    chrome.wait("document.querySelector('#promptZone [data-n=\"7\"]') !== null")
    assert chrome.js("""(() => {
        const zone = document.querySelector('#promptZone');
        const last = zone.querySelector('[data-n="7"]');
        zone.scrollTop = zone.scrollHeight;
        return zone.scrollHeight > zone.clientHeight && zone.scrollWidth <= zone.clientWidth
            && last.getBoundingClientRect().top >= zone.getBoundingClientRect().top
            && last.getBoundingClientRect().bottom <= zone.getBoundingClientRect().bottom
            && document.querySelector('#composer').getBoundingClientRect().bottom <= innerHeight;
    })()""")


def test_last_line_of_long_reply_is_reachable_above_composer(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("done")]
    api.approval = None
    code = "\n".join("value_" * 28 for _ in range(22))
    answer = "\n\n".join(f"{number}. Step\n   - Nested item\n   ```text\n{code}\n   ```" for number in range(1, 6))
    api.messages = [{"role": "assistant", "text": answer + "\n\nFinal response line."}]
    chrome.resize(320, 640)
    open_first(chrome, url)
    assert chrome.js("document.querySelectorAll('#messages pre code').length") == 5
    assert chrome.js("document.querySelector('#messages pre').scrollWidth > document.querySelector('#messages pre').clientWidth")
    for height in (640, 400):
        if height == 400:
            chrome.js("document.querySelector('#message').focus()")
            chrome.resize(320, height)
        chrome.wait(f"innerHeight === {height}")
        assert chrome.js("""(() => {
            const messages = document.querySelector('#messages');
            messages.scrollTop = messages.scrollHeight;
            const tail = messages.querySelector('.msg > p:last-child').lastChild;
            const range = document.createRange();
            range.setStart(tail, tail.textContent.indexOf('Final response line.'));
            range.setEnd(tail, tail.length);
            const line = range.getBoundingClientRect();
            return line.top >= messages.getBoundingClientRect().top
                && line.bottom <= document.querySelector('#composer').getBoundingClientRect().top
                && messages.scrollWidth <= messages.clientWidth;
        })()""")


def test_chat_urls_render_as_safe_clickable_links(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [{"role": "assistant", "text": "Read [the guide](https://example.test/guide), https://example.test/docs, [the PDF](/home/user/api/notes.pdf) and /home/user/api/attached.pdf."}]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages a[href*=example]') !== null")
    assert chrome.js("document.querySelector('#messages a')?.getAttribute('href')") == "https://example.test/guide"
    assert chrome.js("document.querySelector('#messages a')?.rel") == "noopener noreferrer"
    assert chrome.js("new URL(document.querySelectorAll('#messages a')[2].href).searchParams.get('path')") == "/home/user/api/notes.pdf"
    assert chrome.js("new URL(document.querySelectorAll('#messages a')[3].href).searchParams.get('path')") == "/home/user/api/attached.pdf"


def test_slash_command_suggestions_filter_and_fill_composer(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    chrome.js("(() => { const input=document.querySelector('#message'); input.value='/'; input.dispatchEvent(new Event('input',{bubbles:true})); })()")
    chrome.wait("!document.querySelector('#slashOptions').hidden")
    assert chrome.js("[...document.querySelectorAll('#slashOptions [data-slash-cmd]')].some(b => b.dataset.slashCmd === '/clear')")
    chrome.js("(() => { const input=document.querySelector('#message'); input.value='/com'; input.dispatchEvent(new Event('input',{bubbles:true})); })()")
    assert chrome.js("[...document.querySelectorAll('#slashOptions [data-slash-cmd]')].every(b => b.dataset.slashCmd.startsWith('/com'))")
    chrome.click("#slashOptions [data-slash-cmd='/compact']")
    assert chrome.js("document.querySelector('#message').value") == "/compact "


def test_open_chat_picks_up_new_message_without_manual_refresh(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .msg') !== null")
    api.messages = [{"role": "assistant", "text": "Fresh event-free update"}]
    wait_for(lambda: chrome.js("document.querySelector('#messages .msg')?.textContent === 'Fresh event-free update'"), "chat poll observes new message", timeout=3)


def test_visibility_changes_update_server_presence(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    wait_for(lambda: api.presence and api.presence[-1]["visible"], "initial visible presence")
    client_id = api.presence[-1]["client_id"]
    first_count = len(api.presence)
    wait_for(lambda: len(api.presence) > first_count and api.presence[-1]["visible"], "periodic visible heartbeat", timeout=6)
    chrome.js("Object.defineProperty(document, 'hidden', {configurable:true, value:true}); document.dispatchEvent(new Event('visibilitychange'))")
    wait_for(lambda: api.presence and not api.presence[-1]["visible"], "hidden presence")
    assert api.presence[-1]["client_id"] == client_id
    chrome.js("Object.defineProperty(document, 'hidden', {configurable:true, value:false}); document.dispatchEvent(new Event('visibilitychange'))")
    wait_for(lambda: api.presence and api.presence[-1]["visible"], "visible presence restored")
    assert api.presence[-1]["client_id"] == client_id


def test_offline_refresh_retains_cards_and_sse_reconnect_loads_snapshot(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("working")]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.g-working .card') !== null")
    chrome.wait("document.querySelector('#conn').hidden")
    assert api.ready.wait(5)
    api.ready.clear()
    chrome.offline(True)
    api.stream_generation += 1
    open_settings(chrome)
    chrome.click("#sheet #refresh")
    chrome.wait("!document.querySelector('#conn').hidden")
    assert chrome.js("document.querySelector('.g-working .proj').textContent") == "fixture"
    api.cards = [card("unknown", project="updated-while-offline")]
    chrome.offline(False)
    assert api.ready.wait(5)
    chrome.wait("document.querySelector('.g-unknown .proj')?.textContent === 'updated-while-offline'")
    chrome.wait("document.querySelector('#conn').hidden")
    assert chrome.js("document.querySelector('#conn').hidden")


def test_open_chat_retains_draft_and_waits_for_fresh_data_after_sse_ready(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    chrome.wait("document.querySelector('#conn').hidden")
    chrome.js("""(() => {
        const draft = document.querySelector('#message');
        draft.value = 'A draft stays here';
        draft.dispatchEvent(new Event('input', {bubbles: true}));
    })()""")
    api.fail_list = api.fail_chat = True
    api.ready.clear()
    api.stream_generation += 1
    chrome.wait("document.querySelector('#conn').getClientRects().length > 0")
    assert api.ready.wait(6)
    assert api.list_failed.wait(6)
    assert chrome.js("document.querySelector('#conn').getClientRects().length > 0")
    assert chrome.js("document.querySelector('#messages .msg').textContent") == "Example answer"

    api.chat_failed.clear()
    api.fail_list = False
    assert api.chat_failed.wait(6)
    assert chrome.js("document.querySelector('#conn').getClientRects().length > 0")
    api.messages = [{"role": "assistant", "text": "Updated after reconnect"}]
    api.fail_chat = False
    chrome.wait("document.querySelector('#messages .msg').textContent === 'Updated after reconnect'")
    chrome.wait("document.querySelector('#conn').hidden")
    assert chrome.js("document.querySelector('#message').value") == "A draft stays here"


def test_sse_update_survives_a_late_list_snapshot(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#conn')?.hidden && document.querySelector('.g-blocked .card') !== null")
    api.list_started.clear()
    api.list_respond.clear()
    open_settings(chrome)
    chrome.click("#sheet #refresh")
    assert api.list_started.wait(5)
    api.publish_session("unknown")
    chrome.wait("document.querySelector('.g-unknown .card') !== null")
    api.list_respond.set()
    chrome.wait("!document.querySelector('#refresh').classList.contains('loading')")
    assert chrome.js("document.querySelector('.g-unknown .card') !== null")
    assert chrome.js("document.querySelector('.g-blocked .card') === null")


def test_stalled_snapshot_recovers_without_reopening_the_stream(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#conn')?.hidden && document.querySelector('.g-blocked .card') !== null")
    chrome.js("""(() => {
        const schedule = window.setTimeout;
        window.setTimeout = (callback, delay, ...args) => schedule(callback, delay === 15000 ? 150 : delay, ...args);
    })()""")
    api.ready.clear()
    api.list_started.clear()
    api.list_respond.clear()
    open_settings(chrome)
    chrome.click("#sheet #refresh")
    assert api.list_started.wait(5)
    chrome.wait("!document.querySelector('#conn').hidden")
    api.cards = [card("unknown")]
    api.list_respond.set()
    chrome.wait("document.querySelector('#conn')?.hidden && document.querySelector('.g-unknown .card') !== null")
    assert not api.ready.is_set()


def test_dock_button_stays_tappable_over_scrolled_cards(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("working", pane_id=f"fake:p{index}", project=f"project-{index}") for index in range(12)]
    chrome.resize(360, 640)
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('.card').length === 12 && document.getAnimations().every(a => ['pulse', 'blink'].includes(a.animationName))")
    assert chrome.js("""(() => {
        const button = document.querySelector('#newSession');
        const box = button.getBoundingClientRect();
        return [0.1, 0.5, 0.9].every(x => [0.2, 0.5, 0.8].every(y =>
            document.elementFromPoint(box.left + box.width * x, box.top + box.height * y) === button));
    })()""")


def test_receipt_line_follows_the_reply_until_the_agent_has_it(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    assert chrome.js("document.querySelector('#receipt').hidden") is True
    api.receipt = {"id": "r1", "ts": time.time(), "decision": "approve", "delivered": None, "destructive": False}
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#receipt')?.textContent.includes('on its way')")
    api.receipt = {**api.receipt, "delivered": time.time()}
    api.approval = None
    api.cards = [card("working")]
    api.events.append({"type": "receipt", "pane": PANE, "delivered": True})
    chrome.wait("document.querySelector('#receipt')?.textContent.includes('agent received it')")
    assert chrome.js("document.querySelector('#receipt').textContent.startsWith('Approved')")


def test_old_receipt_is_not_shown(api_fixture):
    chrome, api, url = api_fixture
    api.receipt = {"id": "r0", "ts": time.time() - 3600, "decision": "deny", "delivered": time.time() - 3599, "destructive": False}
    open_first(chrome, url)
    assert chrome.js("document.querySelector('#receipt').hidden") is True


def test_decisions_tab_lists_sent_and_refused_answers(api_fixture):
    chrome, api, url = api_fixture
    now = time.time()
    api.audit = [
        {"id": "a2", "ts": now, "pane": PANE, "project": "fixture", "decision": "approve", "outcome": "refused", "reason": "The approval screen changed; refresh",
         "command": "git push origin example", "destructive": True, "login": "user@example.test", "delivered": None},
        {"id": "a1", "ts": now - 60, "pane": PANE, "project": "fixture", "decision": "deny", "outcome": "sent", "reason": "",
         "command": "<img src=x onerror=window.pwned=1>", "destructive": False, "login": "user@example.test", "delivered": now - 59},
    ]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#history') !== null && document.querySelector('.card') !== null")
    chrome.click("#history")
    chrome.wait("document.querySelector('#sheet [data-tab=decisions]') !== null")
    chrome.click("#sheet [data-tab=decisions]")
    chrome.wait("document.querySelectorAll('#sheet .audit').length === 2")
    first, second = chrome.js("[...document.querySelectorAll('#sheet .audit')].map(e => e.textContent)")
    assert "Refused" in first and "The approval screen changed" in first and "user@example.test" in first
    assert "Denied" in second and "agent received it" in second and "<img src=x" in second
    assert chrome.js("window.pwned") is None


def test_a_timed_out_approval_is_on_the_record_and_the_setting_is_visible(api_fixture):
    chrome, api, url = api_fixture
    now = time.time()
    api.audit = [{"id": "t1", "ts": now, "pane": PANE, "project": "fixture", "decision": "deny", "outcome": "timeout", "reason": "No answer in 30 minutes",
                  "command": "npm publish", "destructive": True, "login": "", "delivered": now}]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#settings') !== null && document.querySelector('.card') !== null")
    chrome.click("#settings")
    chrome.wait("document.querySelector('#sheet [data-settings]') !== null")
    assert chrome.js("document.querySelector('#sheet .row.timeout')") is None  # off by default: nothing to say
    api.config = {"quick_label": "", "approval_timeout_minutes": 30}
    chrome.cdp("Page.navigate", url=url + "?again")
    chrome.wait("document.querySelector('#settings') !== null && document.querySelector('.card') !== null")
    chrome.click("#settings")
    chrome.wait("document.querySelector('#sheet .row.timeout') !== null")
    assert chrome.js("document.querySelector('#sheet .row.timeout').textContent") == "Unanswered approvals are denied after 30 min"
    assert chrome.js("document.querySelector('#sheet .row.timeout').tagName") == "DIV"  # shown, not switchable from the phone
    chrome.click("#sheet [data-close]")
    chrome.click("#history")
    chrome.wait("document.querySelector('#sheet [data-tab=decisions]') !== null")
    chrome.click("#sheet [data-tab=decisions]")
    chrome.wait("document.querySelectorAll('#sheet .audit').length === 1")
    text = chrome.js("document.querySelector('#sheet .audit').textContent")
    assert "Denied after 30 min without an answer" in text and "agent received it" in text and "npm publish" in text
    assert "undefined" not in text and "null" not in text


def test_interface_uses_no_emoji_glyphs():
    pictographs = re.compile("[\U0001F000-\U0001FAFF☀-➿⌚-⏿⬀-⯿]")
    for name in ("index.html", "app.js", "i18n.js", "md.js"):
        found = pictographs.findall((WEB / name).read_text())
        assert not found, f"{name} still draws {found}"


def test_theme_colour_is_the_same_everywhere():
    html = (WEB / "index.html").read_text()
    metas = dict(re.findall(r'name="theme-color" media="\(prefers-color-scheme: (dark|light)\)" content="(#[0-9a-fA-F]{6})"', html))
    manifest = json.loads((WEB / "manifest.webmanifest").read_text())
    dark, light = _palettes()
    # The installed app's splash follows the dark ground; the browser bar follows whichever theme is showing.
    assert metas["dark"].lower() == manifest["theme_color"].lower() == manifest["background_color"].lower() == dark["bg"].lower()
    assert metas["light"].lower() == light["bg"].lower() != dark["bg"].lower()


def _palettes():
    css = (WEB / "style.css").read_text()
    dark = dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-fA-F]{6})\b", css.split("{", 1)[1].split("}", 1)[0]))
    block = re.search(r":root\[data-theme=light\]\{([^}]*)\}", css).group(1)
    return dark, dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-fA-F]{6})\b", block))


def _contrast(a, b):
    def lum(h):
        rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, bl = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_both_themes_define_the_same_colours_and_stay_readable():
    dark, light = _palettes()
    assert set(light) == set(dark)  # one set of names, so no rule can fall back to the other theme by accident
    for name, p in (("dark", dark), ("light", light)):
        for ink in ("text", "strong", "muted", "zer", "lal-t", "ok", "run"):
            for ground in ("bg", "bg2", "card", "card2"):
                assert _contrast(p[ink], p[ground]) >= 4.5, f"{name}: --{ink} on --{ground} is {_contrast(p[ink], p[ground]):.2f}"
        # Gold as a filled surface (the seal) carries dark ink in both themes; ruby carries white.
        assert _contrast(p["zer-ink"], p["seal"]) >= 4.5 and _contrast("#ffffff", p["lal"]) >= 4.5, name


def test_light_theme_follows_the_system_setting(api_fixture):
    chrome, api, url = api_fixture
    dark, light = _palettes()
    rgb = lambda h: "rgb({}, {}, {})".format(*(int(h[i:i + 2], 16) for i in (1, 3, 5)))
    chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
    try:
        open_first(chrome, url)
        assert chrome.js("getComputedStyle(document.body).backgroundColor") == rgb(light["bg"])
        assert chrome.js("getComputedStyle(document.body).color") == rgb(light["text"])
        assert chrome.js("getComputedStyle(document.documentElement).colorScheme") == "light"
        chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
        chrome.wait(f"getComputedStyle(document.body).backgroundColor === '{rgb(dark['bg'])}'")  # theme.js hears the change a moment later
        assert chrome.js("getComputedStyle(document.documentElement).colorScheme") == "dark"
    finally:
        chrome.cdp("Emulation.setEmulatedMedia", features=[])


def test_theme_button_overrides_the_system_and_gives_way_when_they_agree(api_fixture):
    chrome, api, url = api_fixture
    dark, light = _palettes()
    rgb = lambda h: "rgb({}, {}, {})".format(*(int(h[i:i + 2], 16) for i in (1, 3, 5)))
    ground = "getComputedStyle(document.body).backgroundColor"
    chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
    try:
        open_first(chrome, url)
        chrome.js("document.querySelector('#back').click()")
        assert chrome.js("document.documentElement.dataset.theme") == "dark"
        assert chrome.js("document.querySelector('#theme').getAttribute('aria-pressed')") == "true"
        chrome.click("#theme")
        chrome.wait(f"{ground} === '{rgb(light['bg'])}'")
        assert chrome.js("localStorage.getItem('theme')") == "light"
        assert chrome.js("document.querySelector('#theme').getAttribute('aria-pressed')") == "false"
        assert chrome.js("document.querySelector('meta[name=theme-color]').content").lower() == light["bg"].lower()
        # The choice outlives a reload, and the first paint is already light.
        chrome.cdp("Page.reload")
        chrome.wait(f"document.readyState === 'complete' && {ground} === '{rgb(light['bg'])}'")
        # The system turns light too: the override has nothing left to say, so the next press follows the system again.
        chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        chrome.click("#theme")
        chrome.wait(f"{ground} === '{rgb(dark['bg'])}'")
        assert chrome.js("localStorage.getItem('theme')") == "dark"
        chrome.click("#theme")
        chrome.wait(f"{ground} === '{rgb(light['bg'])}'")
        assert chrome.js("localStorage.getItem('theme')") is None
    finally:
        chrome.js("localStorage.removeItem('theme')")
        chrome.cdp("Emulation.setEmulatedMedia", features=[])


def test_receipt_draws_the_road_from_sent_to_arrived(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    api.receipt = {"id": "r1", "ts": time.time(), "decision": "approve", "delivered": None, "destructive": False}
    chrome.click(APPROVE)
    chrome.wait("document.querySelectorAll('#receipt .leg').length === 3")
    assert chrome.js("document.querySelectorAll('#receipt .leg.on').length") == 2
    assert chrome.js("document.querySelector('#receipt').getAttribute('aria-label')").endswith("on its way")
    api.receipt = {**api.receipt, "delivered": time.time()}
    api.approval = None
    api.cards = [card("working")]
    api.events.append({"type": "receipt", "pane": PANE, "delivered": True})
    chrome.wait("document.querySelectorAll('#receipt .leg.on').length === 3")
    assert chrome.js("document.querySelector('#receipt .leg:last-child svg') !== null")


def test_destructive_approval_asks_for_two_seals_before_the_first_tap(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    chrome.wait(f"document.querySelector({json.dumps(APPROVE)}) !== null")
    assert chrome.js("document.querySelector('#promptZone .prompt').classList.contains('two-seals')") is False
    assert chrome.js(f"document.querySelector({json.dumps(APPROVE)}).textContent.trim()") == "Approve"
    api.publish_prompt(DESTRUCTIVE)
    chrome.wait("document.querySelector('#promptZone .prompt.two-seals') !== null")
    assert chrome.js("document.querySelector('#promptZone .prompt h3').textContent").startswith("Needs two seals")
    assert chrome.js(f"document.querySelector({json.dumps(APPROVE)}).textContent.trim()") == "Review…"
    chrome.click("#back")
    chrome.wait("document.querySelector('.card.two-seals') !== null")


def test_header_keeps_the_ledger_and_moves_rare_actions_into_settings(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    assert chrome.js("[...document.querySelectorAll('#home .top button')].filter(b => !b.hidden).map(b => b.id)") == ["theme", "history", "settings"]
    chrome.click("#settings")
    chrome.wait("document.querySelector('#sheet #lang') !== null")
    assert chrome.js("['#lang', '#refresh', '[data-close]'].every(q => document.querySelector('#sheet ' + q) !== null)")
    assert chrome.js("document.querySelector('#sheet .grab') !== null")
    chrome.click("#sheet [data-close]")
    chrome.wait("document.querySelector('#sheet').hidden")


def test_always_allow_needs_a_deliberate_second_tap(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt({**APPROVAL, "fingerprint": "with-always", "roles": ["approve", "deny", "always"], "options": [{"role": "approve"}, {"role": "deny"}, {"role": "always"}]})
    open_first(chrome, url)
    chrome.wait("document.querySelector('#promptZone [data-d=always]') !== null")
    assert chrome.js("document.querySelector('#promptZone [data-d=always]').checkVisibility()") is False
    chrome.click("#promptZone details.more summary")
    chrome.wait("document.querySelector('#promptZone [data-d=always]').checkVisibility()")


def test_slash_menu_opens_beside_a_pending_approval_not_over_it(api_fixture):
    chrome, api, url = api_fixture
    chrome.resize(390, 780)
    open_first(chrome, url)
    chrome.wait(f"document.querySelector({json.dumps(APPROVE)}) !== null")
    chrome.js("(() => { const m = document.querySelector('#message'); m.focus(); m.value = '/'; m.dispatchEvent(new Event('input', {bubbles: true})); })()")
    chrome.wait("!document.querySelector('#slashOptions').hidden")
    assert chrome.js("""(() => {
        const menu = document.querySelector('#slashOptions').getBoundingClientRect();
        const prompt = document.querySelector('#promptZone .prompt').getBoundingClientRect();
        return menu.top >= prompt.bottom - 1 || menu.bottom <= prompt.top + 1;
    })()""")
    assert chrome.js("getComputedStyle(document.querySelector('#keybar')).scrollbarWidth") == "none"


def test_very_old_sessions_show_a_date_instead_of_a_day_count(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    assert chrome.js("ago(Date.now() / 1000 - 3 * 86400)") == "3 d"
    old = chrome.js("ago(Date.parse('2023-09-25T12:00:00Z') / 1000)")
    assert "2023" in old and " d" not in old


def test_slash_menu_offers_commands_installed_on_the_machine(api_fixture):
    chrome, api, url = api_fixture
    api.commands = [
        {"name": "/clear", "description": "", "source": "builtin"},
        {"name": "/ship", "description": "<img src=x onerror=window.pwned=1>Ship it", "source": "project"},
        {"name": "/git:sync", "description": "Rebase and push", "source": "user"},
    ]
    open_first(chrome, url)
    chrome.wait("slashCommands().some(c => c.name === '/ship')")
    chrome.js("(() => { const input=document.querySelector('#message'); input.value='/sy'; input.dispatchEvent(new Event('input',{bubbles:true})); })()")
    chrome.wait("!document.querySelector('#slashOptions').hidden")
    assert chrome.js("[...document.querySelectorAll('#slashOptions [data-slash-cmd]')].map(b => b.dataset.slashCmd)") == ["/git:sync"]
    chrome.js("(() => { const input=document.querySelector('#message'); input.value='/s'; input.dispatchEvent(new Event('input',{bubbles:true})); })()")
    assert chrome.js("document.querySelector('#slashOptions [data-slash-cmd=\"/ship\"] small').textContent") == "<img src=x onerror=window.pwned=1>Ship it"
    assert chrome.js("window.pwned") is None
    chrome.click("#slashOptions [data-slash-cmd='/ship']")
    assert chrome.js("document.querySelector('#message').value") == "/ship "


def test_agent_screen_is_shown_read_only_and_follows_the_terminal(api_fixture):
    chrome, api, url = api_fixture
    api.screen = "$ pytest -q\n<b onclick=window.pwned=1>12 passed</b>"
    open_first(chrome, url)
    chrome.click("#sMenu")
    chrome.click("#sheet [data-s]")
    chrome.wait("document.querySelector('#sheet pre.term')?.textContent.includes('12 passed')")
    assert chrome.js("document.querySelector('#sheet pre.term').textContent") == api.screen
    assert chrome.js("document.querySelector('#sheet pre.term b') === null")
    assert chrome.js("document.querySelectorAll('#sheet input, #sheet textarea, #sheet [data-k]').length") == 0
    api.screen = "$ pytest -q\n13 passed"
    chrome.wait("document.querySelector('#sheet pre.term')?.textContent.endsWith('13 passed')")
    assert not api.replies


def test_changes_sheet_shows_files_and_a_coloured_diff_without_any_input(api_fixture):
    chrome, api, url = api_fixture
    long_path = "packages/billing/src/retries/" + "very_long_directory_name/" * 4 + "backoff_with_jitter.py"
    api.changes = {
        "repo": True, "branch": "retry-backoff", "more_files": 4, "truncated": True,
        "files": [{"code": "M", "path": long_path}, {"code": "?", "path": "<img src=x onerror=window.pwned=1>.txt"}],
        "diff": "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n one\n-two\n+<b onclick=window.pwned=1>three</b>" + " and a very long line" * 30 + "\n",
    }
    chrome.resize(320, 700)
    open_first(chrome, url)
    chrome.click("#sMenu")
    chrome.click("#sheet [data-d]")
    chrome.wait("document.querySelectorAll('#sheet .files li').length === 2")
    assert chrome.js("document.querySelector('#sheet h3').textContent") == "Changes"
    assert "retry-backoff" in chrome.js("document.querySelector('#sheet .note').textContent")
    assert chrome.js("[...document.querySelectorAll('#sheet .files li b')].map(b => b.textContent)") == ["M", "?"]
    assert chrome.js("document.querySelector('#sheet .files li:last-child span').textContent") == "<img src=x onerror=window.pwned=1>.txt"
    assert "4" in chrome.js("document.querySelector('#sheet .files-more').textContent")
    assert chrome.js("[...document.querySelectorAll('#sheet pre.diff span')].map(e => e.className)") == ["h", "h", "h", "at", "", "del", "add"]
    assert chrome.js("document.querySelector('#sheet pre.diff').textContent") == api.changes["diff"].rstrip("\n")
    assert chrome.js("document.querySelector('#sheet pre.diff b') === null") and chrome.js("window.pwned") is None
    assert chrome.js("!document.querySelector('#sheet .cut').hidden")
    assert chrome.js("document.querySelectorAll('#sheet input, #sheet textarea, #sheet [data-k]').length") == 0
    # Long paths and long lines stay inside the sheet at 320 px: the path wraps, the diff scrolls on its own.
    assert chrome.js("document.documentElement.scrollWidth") <= 320
    assert chrome.js("(() => { const edge=document.querySelector('#sheet').getBoundingClientRect().right; return [...document.querySelectorAll('#sheet .files li, #sheet pre.diff')].every(e => e.getBoundingClientRect().right <= edge); })()")
    assert chrome.js("(() => { const p=document.querySelector('#sheet pre.diff'); return p.scrollWidth > p.clientWidth; })()")
    assert not api.replies


def test_changes_sheet_has_plain_empty_states(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    chrome.click("#sMenu")
    chrome.click("#sheet [data-d]")
    chrome.wait("document.querySelector('#sheet .empty')?.textContent === 'This folder is not a git repository.'")
    assert chrome.js("document.querySelector('#sheet pre.diff') === null")
    api.changes = dict(api.changes, repo=True, branch="main")
    chrome.click("#sheet [data-close]")
    chrome.click("#sMenu")
    chrome.click("#sheet [data-d]")
    chrome.wait("document.querySelector('#sheet .empty')?.textContent === 'Nothing has changed since the last commit.'")
    # Only new, untracked files: the list shows them and says why there is no diff.
    api.changes = dict(api.changes, files=[{"code": "?", "path": "notes.md"}])
    chrome.click("#sheet [data-close]")
    chrome.click("#sMenu")
    chrome.click("#sheet [data-d]")
    chrome.wait("document.querySelectorAll('#sheet .files li').length === 1")
    assert chrome.js("document.querySelector('#sheet .empty').textContent") == "New files are listed above; their contents are not shown."


def test_subagents_show_as_cards_with_their_state(api_fixture):
    chrome, api, url = api_fixture
    long = "Map every caller of the retry helper across the billing and invoicing packages " * 2
    api.messages = [
        {"role": "assistant", "text": "Sending three out."},
        {"role": "tool", "tool": "Bash", "text": "ls"},
        {"role": "tool", "tool": "Agent", "text": long.strip(), "agent": {"kind": "Explore", "state": "running"}},
        {"role": "tool", "tool": "Task", "text": "Check the changelog", "agent": {"kind": "", "state": "done"}, "result": "<i onclick=window.pwned=1>Two entries</i>"},
        {"role": "tool", "tool": "Agent", "text": "Probe staging", "agent": {"kind": "general-purpose", "state": "failed"}},
    ]
    chrome.resize(320, 700)
    open_first(chrome, url)
    chrome.wait("document.querySelectorAll('#messages details.sub').length === 3")
    assert chrome.js("document.querySelectorAll('#messages details.tool:not(.sub)').length") == 1
    assert chrome.js("[...document.querySelectorAll('#messages details.sub')].map(d => d.dataset.state)") == ["running", "done", "failed"]
    assert chrome.js("[...document.querySelectorAll('#messages details.sub .sub-state')].map(e => e.textContent)") == ["Working", "Done", "Failed"]
    assert chrome.js("document.querySelector('#messages details.sub .sub-kind').textContent") == "Subagent · Explore"
    assert chrome.js("document.querySelectorAll('#messages details.sub')[1].querySelector('.sub-kind').textContent") == "Subagent"
    # The whole description is readable at 320 px and nothing spills out of the column.
    assert chrome.js("(() => { const e=document.querySelector('#messages details.sub .sub-what'); return e.scrollWidth <= e.clientWidth + 1 && e.scrollHeight <= e.clientHeight + 1; })()")
    assert chrome.js("document.documentElement.scrollWidth") <= 320
    assert chrome.js("(() => { const edge=document.querySelector('#messages').getBoundingClientRect().right; return [...document.querySelectorAll('#messages details.sub')].every(d => d.getBoundingClientRect().right <= edge - 8); })()")
    assert chrome.js("document.querySelector('#messages details.sub i') === null")
    # A card that is still out keeps its place when it comes back: same element, new state.
    chrome.js("window.firstSub = document.querySelector('#messages details.sub')")
    api.messages = [dict(m, agent=dict(m["agent"], state="done")) if m.get("agent", {}).get("state") == "running" else m for m in api.messages]
    wait_for(lambda: chrome.js("document.querySelector('#messages details.sub').dataset.state") == "done", "running subagent turns done", timeout=4)
    assert chrome.js("document.querySelector('#messages details.sub') === window.firstSub")
    assert chrome.js("document.querySelector('#messages details.sub .sub-state').textContent") == "Done"


def test_a_subagent_the_record_lost_track_of_is_not_shown_as_working(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [{"role": "assistant", "text": "Sending one out."}, {"role": "tool", "tool": "Agent", "text": "Run the slow suite", "state": "unknown", "agent": {"kind": "Explore", "state": "unknown"}}]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages details.sub') !== null")
    assert chrome.js("document.querySelector('#messages details.sub').dataset.state") == "unknown"
    assert chrome.js("document.querySelector('#messages details.sub .sub-state').textContent") == "No result in the record"
    assert chrome.js("getComputedStyle(document.querySelector('#messages details.sub .sub-mark')).animationName") == "none"


def test_answer_stays_in_the_chat_when_nothing_else_is_waiting(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    api.receipt = {"id": "r1", "ts": time.time(), "decision": "approve", "delivered": None, "destructive": False}
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#receipt')?.textContent.includes('on its way')")
    api.approval = None
    api.cards = [card("working")]
    time.sleep(4.5)  # it used to leave for the home screen once the agent moved on, so the road to the agent was never seen
    assert chrome.js("!document.querySelector('#session').hidden && !document.querySelector('#receipt').hidden")


def test_relative_paths_link_whole_and_links_follow_the_theme(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [{"role": "assistant", "text": "The notes are in docs/releases/2.4.md, the code in src/app.py, the rest in ./notes.md and at https://example.test/a.md."}]
    open_first(chrome, url)
    chrome.wait("document.querySelectorAll('#messages a').length === 3")
    links = chrome.js("[...document.querySelectorAll('#messages a')].map(a => [a.textContent, new URL(a.href).searchParams.get('path')])")
    assert links == [["docs/releases/2.4.md", "docs/releases/2.4.md"], ["./notes.md", "./notes.md"], ["https://example.test/a.md", None]]
    assert chrome.js("getComputedStyle(document.querySelector('#messages a')).color === getComputedStyle(document.querySelector('#messages a').parentElement).color")


def test_a_plain_shell_pane_has_nothing_to_write_with(api_fixture):
    chrome, api, url = api_fixture
    api.approval = None
    api.cards = [dict(card("idle"), agent="terminal")]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#composer').hidden && !document.querySelector('#plainNote').hidden")
    assert chrome.js("document.querySelector('#keybar').hidden")
    assert "no agent" in chrome.js("document.querySelector('#plainNote').textContent")
    # #session slides in, so measure the note against its own screen, not the window
    assert chrome.js("(() => { const n=document.querySelector('#plainNote').getBoundingClientRect(), s=document.querySelector('#session').getBoundingClientRect(); return n.height > 0 && n.left >= s.left && n.right <= s.right && n.bottom <= s.bottom; })()")
    # The moment Herdr names the agent, the composer is back.
    api.cards = [card("idle")]
    api.events.append({"type": "session", "session": api.cards[0]})
    chrome.wait("!document.querySelector('#composer').hidden && document.querySelector('#plainNote').hidden")


def test_approving_presses_the_seal_onto_the_question_and_the_road_starts_from_it(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    assert chrome.js("document.querySelector('#promptZone .stamp')") is None
    api.receipt = {"id": "r1", "ts": time.time(), "decision": "approve", "delivered": None, "destructive": False}
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#promptZone .prompt.stamped .stamp svg') !== null")
    # The same question arriving again keeps its one seal.
    api.publish_prompt({**APPROVAL})
    time.sleep(.6)
    assert chrome.js("document.querySelectorAll('#promptZone .prompt.stamped .stamp').length") == 1
    chrome.wait("document.querySelector('#receipt .leg:first-child svg') !== null")
    # A different question has not been answered: no seal on it.
    api.publish_prompt(OTHER_APPROVAL)
    chrome.wait("document.querySelector('#promptZone pre')?.textContent === 'pytest -q test_web.py'")
    assert chrome.js("document.querySelector('#promptZone .stamp')") is None


def test_refusing_leaves_no_seal(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    api.receipt = {"id": "r1", "ts": time.time(), "decision": "deny", "delivered": None, "destructive": False}
    chrome.click('#promptZone [data-d="deny"]')
    chrome.wait("document.querySelector('#receipt')?.textContent.includes('on its way')")
    assert chrome.js("document.querySelector('.stamp')") is None
    assert chrome.js("document.querySelector('#receipt .leg:first-child svg')") is None


def test_the_mark_is_drawn_once_when_the_app_opens(api_fixture):
    chrome, api, url = api_fixture
    assert 'class="mark draw"' in (WEB / "index.html").read_text()
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card-open') !== null")
    wait_for(lambda: chrome.js("!document.querySelector('#home .mark').classList.contains('draw')"), "the mark finished drawing", timeout=4)


def test_card_without_a_known_time_does_not_end_in_a_dangling_dot(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("working", since=0), card("done", pane_id="fake:p2", bucket="idle", since=0)]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('.card').length === 2")
    assert chrome.js("[...document.querySelectorAll('.card .state')].map(e => e.textContent)") == ["Working", "Done"]


def test_card_title_row_is_as_tall_as_its_text_and_the_pin_stays_a_full_target(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("working")]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card .pin') !== null")
    assert chrome.js("document.querySelector('.card .c-top').getBoundingClientRect().height") <= 26
    assert chrome.js("(() => { const b = document.querySelector('.card .pin').getBoundingClientRect(); return Math.min(b.width, b.height); })()") >= 44
    assert chrome.js("(() => { const p = document.querySelector('.card .pin'), b = p.getBoundingClientRect(); return document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2) === p || p.contains(document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2)); })()")


def test_top_and_bottom_edges_are_flat_ground_with_no_colour_fade(api_fixture):
    # A fade at the top or bottom edge shows as bands on the phone (Taha, 28.11.2026): flat ground, and a line above the dock.
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    image = "['#home', '#home .top', '.dock'].map(s => getComputedStyle(document.querySelector(s)).backgroundImage)"
    for theme in ("dark", "light"):
        chrome.js(f"document.documentElement.dataset.theme = '{theme}'")
        for under in ("add", "remove"):
            chrome.js(f"document.documentElement.classList.{under}('under')")
            assert chrome.js(image) == ["none"] * 3, (theme, under)
    assert chrome.js("(() => { const d = getComputedStyle(document.querySelector('.dock')), h = getComputedStyle(document.querySelector('#home')); return d.backgroundColor === h.backgroundColor && d.borderTopWidth === '1px'; })()")
    # the bar starts close above its buttons: nothing see-through is left over the list to swallow a tap
    assert chrome.js("document.querySelector('#newSession').getBoundingClientRect().top - document.querySelector('.dock').getBoundingClientRect().top") <= 12


def test_theme_button_is_not_a_second_logo():
    # Day is a plain round sun. The seal outline with its sharp corner belongs to the mark alone.
    for path in ("web/index.html", "web/app.js", "site/index.html", "site/tr/index.html"):
        text = (WEB.parent / path).read_text()
        assert '<circle class="halo"' in text and 'class="halo" d=' not in text, path


def test_starting_something_new_is_ink_not_black_and_not_gold():
    dark, light = _palettes()
    for p in (dark, light):
        assert p["ink"] != p["seal"] and _contrast(p["bg"], p["ink"]) >= 4.5
    assert light["ink"] != light["strong"]
    assert ".pill-btn.primary{background:var(--ink)" in (WEB.parent / "web/style.css").read_text()


def test_idle_heading_is_a_text_row_and_its_toggle_stays_a_full_target(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("working"), card("idle", pane_id="fake:p2", bucket="idle")]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.idle-toggle') !== null")
    assert chrome.js("document.querySelector('.g-idle h2').getBoundingClientRect().height") <= 24
    assert chrome.js("document.querySelector('.idle-toggle').getBoundingClientRect().height") >= 44
    assert chrome.js("(() => { const p = document.querySelector('.idle-toggle'), b = p.getBoundingClientRect(); const e = document.elementFromPoint(b.left + 40, b.top + b.height / 2); return e === p || p.contains(e); })()")


def test_three_providers_fit_the_limit_strip_on_a_phone(api_fixture):
    chrome, api, url = api_fixture
    api.limits = [{"label": name, "updated": time.time(), "items": [{"label": "7d", "used": used, "resets": 0}]} for name, used in (("Claude", 12), ("Codex", 74), ("agy", 100))]
    chrome.resize(390, 780)
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('#limits .lim').length === 3")
    assert chrome.js("(() => { const b = document.querySelector('#limits'); return b.scrollWidth <= b.clientWidth; })()")
    assert chrome.js("[...document.querySelectorAll('#limits .lim')].map(e => e.textContent)") == ["Claude12%", "Codex74%", "agy100%"]


def test_the_mark_is_one_wax_seal_everywhere_and_no_shape_keeps_a_sharp_corner():
    root = WEB.parent
    icon = (WEB / "icon.svg").read_text()
    wax = re.search(r'<path id="wax" d="(M[^"]+z)"', (WEB / "index.html").read_text()).group(1)
    assert wax.count("L") == 71  # an uneven edge, pressed by hand, not a circle
    for path in ("site/index.html", "site/tr/index.html"):
        assert f'<path id="wax" d="{wax}"' in (root / path).read_text(), path
    assert wax in icon
    for path in ("web/index.html", "web/app.js", "web/icon.svg", "site/index.html", "site/tr/index.html"):
        text = (root / path).read_text()
        assert "M16 4h12" not in text and "M12 3.5h8.5" not in text and "h160v160" not in text, path
    for path in ("web/style.css", "site/site.css"):
        css = (root / path).read_text()
        assert not re.search(r"border-radius:\d+px \d+px \d+px \d+px", css), path
        assert "corner" not in css, path


def test_daylight_is_warm_paper_without_pink_and_the_site_wears_the_same_colours():
    dark, light = _palettes()
    r, g, b = (int(light["bg"][i:i + 2], 16) for i in (1, 3, 5))
    assert r >= g >= b and r - g <= 4, light["bg"]  # pink and mauve both push green below red and blue
    site = (WEB.parent / "site" / "site.css").read_text()
    site_dark = dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-fA-F]{6})\b", site.split(":root{", 1)[1].split("}", 1)[0]))
    site_light = dict(re.findall(r"--([a-z0-9-]+):(#[0-9a-fA-F]{6})\b", re.search(r":root\[data-theme=light\]\{([^}]*)\}", site).group(1)))
    for mine, theirs in ((site_dark, dark), (site_light, light)):
        assert mine and all(theirs[k] == v for k, v in mine.items())
    for page in ("index.html", "tr/index.html"):
        assert f'content="{light["bg"]}" media="(prefers-color-scheme: light)"' in (WEB.parent / "site" / page).read_text()


def test_home_screen_app_on_iphone_draws_its_own_ground_under_the_clock(api_fixture):
    # The bar iOS draws above a home-screen app does not follow the theme the app's own button picks (measured 26.11.2026:
    # a light page under a dark bar). So the page goes under the clock itself, and nothing above the glow fakes a bar.
    chrome, api, url = api_fixture
    assert 'name="apple-mobile-web-app-status-bar-style" content="black-translucent"' in (WEB / "index.html").read_text()
    open_first(chrome, url)
    layers = "getComputedStyle(document.querySelector('#home')).backgroundImage.split('gradient(').length - 1"
    assert chrome.js(layers) == 0  # flat ground in a browser tab
    # Drawing under the bar leaves the webview short at the bottom on iOS; the document asks for the missing height,
    # and the bottom inset is dropped only while app.js still measures the page as short (.sa).
    assert chrome.js("getComputedStyle(document.documentElement).minHeight") in ("0px", "auto")
    chrome.js("document.documentElement.classList.add('under')")
    assert chrome.js(layers) == 0  # and under the clock: the same flat ground, up to the top
    assert chrome.js("getComputedStyle(document.documentElement).minHeight") == "100%"  # 100% + a top inset that is 0 here
    chrome.js("document.documentElement.classList.add('sa')")
    assert chrome.js("getComputedStyle(document.querySelector('.dock')).paddingBottom") == "10px"


def test_the_browser_bars_on_iphone_can_follow_the_theme():
    # Measured in a Safari tab on 27.11.2026: Safari colours the areas above and below the page from what it finds at the
    # edges. A fixed body kept them in the colour the page opened with (light page, dark bars), and the connection strip at
    # the very top lent them its own darker ground. So the body is not fixed, #app does the clipping, and the strip sits on --bg.
    css = (WEB / "style.css").read_text()
    body = re.search(r"^body\{([^}]*)\}", css, re.M).group(1)
    assert "position:fixed" not in body
    assert "overflow:hidden" in re.search(r"^#app\{([^}]*)\}", css, re.M).group(1)
    assert "background:var(--bg)}" in re.search(r"^\.conn\{[^}]*\}", css, re.M).group(0)


def test_placeholder_text_is_as_readable_as_other_quiet_text(api_fixture):
    chrome, _api, url = api_fixture
    for scheme in ("dark", "light"):
        chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}])
        open_first(chrome, url)
        placeholder, muted = chrome.js(
            "(() => { const el = document.createElement('i'); el.style.color = 'var(--muted)'; document.body.append(el);"
            " return [getComputedStyle(document.querySelector('#message'), '::placeholder').color, getComputedStyle(el).color]; })()")
        assert placeholder == muted, scheme



def test_jump_to_new_message_button_speaks_the_chosen_language(api_fixture):
    chrome, _api, url = api_fixture
    for lang, label in (("en", "↓ New"), ("tr", "↓ Yeni")):
        chrome.cdp("Page.navigate", url=f"{url}/?{lang}")
        chrome.wait("document.querySelector('#jump') !== null && document.readyState === 'complete'")
        chrome.js(f"localStorage.setItem('lang', '{lang}')")
        chrome.cdp("Page.navigate", url=f"{url}/?{lang}2")
        chrome.wait(f"document.readyState === 'complete' && document.documentElement.lang === '{lang}'")
        assert chrome.js("document.querySelector('#jump').textContent") == label



def test_banner_says_why_the_session_list_could_not_be_read(api_fixture):
    chrome, api, url = api_fixture
    api.fail_list = True
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#conn')?.textContent === 'Fixture snapshot unavailable'")
    api.fail_list = False
    chrome.wait("document.querySelector('#conn').hidden && document.querySelector('.card') !== null")



def test_keyboard_focus_follows_into_a_session_and_back_to_its_card(api_fixture):
    chrome, _api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card-open') !== null")
    chrome.js("document.querySelector('.card-open').focus()")
    chrome.key("Enter", "Enter", 13)
    chrome.wait("!document.querySelector('#session').hidden")
    assert chrome.js("document.activeElement.id") == "back"
    chrome.key("Enter", "Enter", 13)
    chrome.wait("!document.querySelector('#home').hidden")
    assert chrome.js("document.activeElement.className") == "card-open"



def test_opening_a_session_by_address_does_not_move_focus(api_fixture):
    chrome, _api, url = api_fixture
    chrome.cdp("Page.navigate", url=f"{url}/#/s/{PANE}")
    chrome.wait("document.querySelector('#messages .msg') !== null")
    assert chrome.js("document.activeElement === document.body")


def test_command_suggestions_are_named_in_the_chosen_language(api_fixture):
    chrome, _api, url = api_fixture
    chrome.cdp("Page.navigate", url=f"{url}/?lang=tr")
    chrome.wait("document.readyState === 'complete' && document.documentElement.lang === 'tr'")
    assert chrome.js("document.querySelector('#slashOptions').getAttribute('aria-label')") == "Eğik çizgi komutları"


def test_a_tap_does_not_leave_a_focus_ring_on_the_next_screen(api_fixture):
    chrome, _api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card-open') !== null")

    def tap(selector):
        x, y = chrome.js(f"(r => [r.left + 20, r.top + r.height / 2])(document.querySelector({json.dumps(selector)}).getBoundingClientRect())")
        for kind in ("mousePressed", "mouseReleased"):
            chrome.cdp("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    tap(".card-open")
    chrome.wait("!document.querySelector('#session').hidden")
    assert chrome.js("document.querySelector(':focus-visible') === null")
    tap("#back")
    chrome.wait("!document.querySelector('#home').hidden")
    assert chrome.js("document.querySelector(':focus-visible') === null")


def test_a_command_too_long_for_its_card_fades_out_instead_of_being_sliced(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt({**APPROVAL, "details": "\n".join(f"echo step {n}" for n in range(12))})
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card .c-cmd') !== null")
    assert chrome.js("getComputedStyle(document.querySelector('.c-cmd')).maskImage").startswith("linear-gradient")
    api.publish_prompt(APPROVAL)
    chrome.wait("document.querySelector('.c-cmd.full-command') !== null")
    assert chrome.js("getComputedStyle(document.querySelector('.c-cmd')).maskImage") == "none"


def test_a_quiet_screen_is_not_redrawn(api_fixture):
    # The clock ticks every 1.5 s; with nothing new to show, nothing on screen may be written again (it costs battery and drops selections).
    chrome, _api, url = api_fixture
    watch = ("window.__changed = []; new MutationObserver(list => list.forEach(r => { const el = r.target.nodeType === 3 ? r.target.parentElement : r.target;"
             " __changed.push(`${r.type} ${el.id || el.getAttribute('class')} ${r.attributeName || ''}`); }))"
             ".observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true})")
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null && document.querySelector('#conn').hidden")
    for screen in ("home", "session"):
        if screen == "session":
            chrome.click(".card-open")
            chrome.wait("document.querySelector('#messages .msg') !== null")
        time.sleep(1.8)  # the first tick and the entrance are over
        chrome.js(watch)
        time.sleep(3.2)
        assert chrome.js("__changed") == [], screen


@pytest.mark.parametrize("cards", [[], [card("idle")]], ids=["no sessions", "folded idle group"])
def test_a_quiet_list_without_active_sessions_is_not_redrawn_either(api_fixture, cards):
    chrome, api, url = api_fixture
    api.cards = [{**c, "since": time.time()} for c in cards]  # stamped now: an older card rightly rewrites its age as minutes pass
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#groups')?.children.length > 0 && document.querySelector('#conn')?.hidden")
    time.sleep(1.8)
    chrome.js("window.__changed = 0; new MutationObserver(list => { __changed += list.length; })"
              ".observe(document.querySelector('#home'), {subtree: true, childList: true, attributes: true, characterData: true})")
    time.sleep(3.2)
    assert chrome.js("__changed") == 0


def send(chrome, text):
    chrome.js(f"(() => {{ const t = document.querySelector('#message'); t.value = {json.dumps(text)}; t.dispatchEvent(new Event('input')); document.querySelector('#composer').requestSubmit(); }})()")


PENDING = "document.querySelector('#messages .msg.pending')"
MINE = "[...document.querySelectorAll('#messages .msg.user')].map(e => e.querySelector('.body')?.textContent || e.textContent)"


def test_sent_message_appears_at_once_and_settles_when_the_agent_has_it(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.respond.clear()
    open_first(chrome, url)
    send(chrome, "run the tests")
    chrome.wait(f"{PENDING}?.textContent.includes('run the tests') && {PENDING}.textContent.includes('Sending')")
    assert chrome.js("document.querySelector('#message').value") == ""
    api.respond.set()
    chrome.wait(f"{PENDING}.textContent.includes('waiting for the agent')")
    api.messages = api.messages + [{"role": "user", "text": "run the tests", "ts": "t1"}]
    chrome.wait(f"!{PENDING}")
    assert chrome.js(MINE) == ["run the tests"]


def test_failed_send_returns_the_draft(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.fail_prompt = True
    open_first(chrome, url)
    send(chrome, "keep me")
    chrome.wait("document.querySelector('#message').value === 'keep me'")
    assert chrome.js(f"!!{PENDING}") is False


def test_repeating_an_earlier_message_waits_for_the_new_copy(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.messages = [{"role": "user", "text": "go on", "ts": "t0"}]
    open_first(chrome, url)
    chrome.wait("!!document.querySelector('#messages .msg.user')")
    send(chrome, "go on")
    chrome.wait(f"{PENDING}?.textContent.includes('waiting for the agent')")
    time.sleep(2)  # a poll that brings only the old copy
    assert chrome.js(f"!!{PENDING}") is True
    api.messages = api.messages + [{"role": "user", "text": "go on", "ts": "t1"}]
    chrome.wait(f"!{PENDING}")


def test_unseen_message_says_so_and_a_command_settles_it(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    chrome.js("window.UNSEEN_MS = 300")
    send(chrome, "/clear")
    chrome.wait(f"{PENDING}?.textContent.includes('Not in the conversation yet')")
    api.messages = [{"role": "command", "text": "/clear", "ts": "t2"}]
    chrome.wait(f"!{PENDING} && document.querySelector('#messages .msg.command')?.textContent === '/clear'")


STORED = "localStorage.getItem('outbox:fake:p1')"
STATUS = f"{PENDING}?.dataset.status"
MAIL_ACTS = f"[...{PENDING}.querySelectorAll('[data-mail]')].filter(b => b.offsetParent).map(b => b.textContent)"


def caught_mid_send(chrome, api, url, text):
    api.publish_session("idle")
    api.respond.clear()
    open_first(chrome, url)
    send(chrome, text)
    chrome.wait(f"{STATUS} === 'sending' && !!{STORED}")
    open_first(chrome, url)  # the app is closed and opened again while the message is still on its way
    api.respond.set()
    chrome.wait(f"{STATUS} === 'unsent'")


def test_a_message_caught_mid_send_comes_back_as_not_sent_and_is_never_resent(api_fixture):
    chrome, api, url = api_fixture
    caught_mid_send(chrome, api, url, "run the tests")
    assert chrome.js(f"{PENDING}.querySelector('.body').textContent") == "run the tests"
    assert "Not sent" in chrome.js(f"{PENDING}.querySelector('small').textContent")
    assert chrome.js(MAIL_ACTS) == ["Try again", "Delete"]
    time.sleep(3)
    assert api.prompts == ["run the tests"] and chrome.js(STATUS) == "unsent"


def test_retry_sends_once_and_delete_removes(api_fixture):
    chrome, api, url = api_fixture
    caught_mid_send(chrome, api, url, "once more")
    chrome.js("window.UNSEEN_MS = 300")
    api.respond.clear()
    chrome.click(f"#messages .msg.pending [data-mail=retry]")
    chrome.wait(f"{STATUS} === 'sending'")
    assert chrome.js(MAIL_ACTS) == []
    api.respond.set()
    chrome.wait(f"{STATUS} === 'unseen'")
    assert api.prompts == ["once more", "once more"]
    chrome.click("#messages .msg.pending [data-mail=drop]")
    chrome.wait(f"!{PENDING}")
    assert chrome.js(STORED) is None and api.prompts == ["once more", "once more"]


def test_an_unseen_message_is_not_lost_to_a_stray_tap(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    chrome.js("window.UNSEEN_MS = 300")
    send(chrome, "do not lose me")
    chrome.wait(f"{STATUS} === 'unseen'")
    assert chrome.js(MAIL_ACTS) == ["Try again", "Delete"]
    chrome.click("#messages .msg.pending .body")
    time.sleep(0.5)
    assert chrome.js(STATUS) == "unseen"
    chrome.click("#messages .msg.pending [data-mail=drop]")
    chrome.wait(f"!{PENDING}")


def test_a_copy_already_in_the_chat_is_not_taken_for_the_new_message(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.messages = [{"role": "user", "text": "ok", "ts": "t0"}]
    api.fail_chat = True  # the conversation has not loaded when the message is sent
    chrome.cdp("Page.navigate", url=url)
    chrome.click(".card-open")
    chrome.wait("!document.querySelector('#session').hidden")
    send(chrome, "ok")
    chrome.wait(f"{STATUS} === 'sent'")
    api.fail_chat = False
    chrome.wait(f"{MINE}.length === 2")
    time.sleep(2)  # polls that bring only the old copy
    assert chrome.js(f"!!{PENDING}") is True
    api.messages = api.messages + [{"role": "user", "text": "ok", "ts": "t1"}]
    chrome.wait(f"!{PENDING}")
    assert chrome.js(STORED) is None


def test_outbox_of_another_conversation_or_closed_pane_is_not_shown(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    record = lambda conversation: json.dumps([{"id": "a", "text": "from before", "status": "sent", "seen": [], "count": 0, "conversation": conversation, "ts": 1}])
    chrome.js(f"localStorage.setItem('outbox:fake:p1', {json.dumps(record('another'))}); localStorage.setItem('outbox:fake:gone', {json.dumps(record('one'))})")
    open_first(chrome, url)
    chrome.wait(f"{STORED} === null && localStorage.getItem('outbox:fake:gone') === null")
    assert chrome.js(f"!!{PENDING}") is False


def _stored_mail(chrome, **fields):
    record = json.dumps([{"id": "a", "text": "from before", "status": "sent", "seen": [], "count": 0, "conversation": None, "ts": 1, **fields}])
    chrome.js(f"localStorage.setItem('outbox:fake:p1', {json.dumps(record)})")


def test_outbox_of_an_earlier_pane_with_the_same_id_is_not_shown(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("idle", born="second")]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    _stored_mail(chrome, born="first")  # written before the conversation had loaded, to a pane that has since closed
    open_first(chrome, url)
    chrome.wait(f"{STORED} === null")
    assert chrome.js(f"!!{PENDING}") is False


def test_outbox_of_this_pane_is_kept_and_a_new_message_carries_its_mark(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("idle", born="second")]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    _stored_mail(chrome, born="second")
    open_first(chrome, url)
    chrome.wait(f"{STATUS} === 'unseen'")
    chrome.click("#messages .msg.pending [data-mail=drop]")
    chrome.wait(f"!{PENDING}")
    api.respond.clear()
    send(chrome, "a new one")
    chrome.wait(f"{STATUS} === 'sending' && !!{STORED}")
    assert chrome.js(f"JSON.parse({STORED})[0].born") == "second"
    api.respond.set()


def test_outbox_is_not_drawn_before_the_conversation_has_been_checked(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    _stored_mail(chrome, conversation="another")
    api.fail_chat = True  # the list is here, the conversation is not: nothing says yet whose words these are
    chrome.cdp("Page.navigate", url=url)
    chrome.click(".card-open")
    chrome.wait("!document.querySelector('#session').hidden")
    time.sleep(2)  # list polls come and go
    assert chrome.js(f"!!{PENDING}") is False and chrome.js(f"{STORED} !== null") is True
    api.fail_chat = False
    chrome.wait(f"{STORED} === null")
    assert chrome.js(f"!!{PENDING}") is False


def test_outbox_works_without_storage(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    chrome.js("Storage.prototype.setItem = () => { throw new Error('full'); }; Storage.prototype.removeItem = () => { throw new Error('full'); }")
    send(chrome, "still goes")
    chrome.wait(f"{STATUS} === 'sent'")
    assert api.prompts == ["still goes"] and chrome.js("document.querySelector('#toasts .err')") is None


def test_each_group_lists_the_latest_conversation_first(api_fixture):
    chrome, api, url = api_fixture
    def one(pane, status, since, active, project):
        return {**card(status), "pane_id": pane, "since": since, "active": active, "project": project, "workspace_label": project}
    api.cards = [one("a", "idle", 900, 100, "old"), one("b", "idle", 100, 500, "new"),
                 one("c", "idle", 800, 700, "pin-quiet"), one("d", "blocked", 50, 60, "pin-waiting")]
    api.pins = ["pin-quiet|pin-quiet", "pin-waiting|pin-waiting"]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('.card').length === 4")
    chrome.wait("!!document.querySelector('.g-pinned')")
    names = lambda group: chrome.js(f"[...document.querySelectorAll('.g-{group} .proj')].map(e => e.textContent)")
    assert names("pinned") == ["pin-waiting", "pin-quiet"] and names("idle") == ["new", "old"]


def test_idle_is_grey_done_is_green_and_working_blinks_in_its_own_colour(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [{**card(status), "pane_id": status, "project": status} for status in ("working", "done", "idle")]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('.card').length === 3")
    token = lambda name: chrome.js(f"(() => {{ const p = document.createElement('i'); p.style.color = 'var(--{name})'; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; }})()")
    colour = lambda status: chrome.js(f"getComputedStyle(document.querySelector('.card.b-{status} .state')).color")
    assert (colour("idle"), colour("done"), colour("working")) == (token("muted"), token("ok"), token("run"))
    assert len({colour("idle"), colour("done"), colour("working")}) == 3
    blink = "getComputedStyle(document.querySelector('.b-working .blink')).animationName"
    assert chrome.js("document.querySelector('.b-working .blink').textContent") == "Working" and chrome.js(blink) != "none"
    chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
    assert chrome.js(blink) == "none"


def said(prefix, n):
    return [{"role": "assistant", "text": f"{prefix} {i}", "ts": f"{prefix}{i}"} for i in range(n)]


TEXTS = "[...document.querySelectorAll('#messages .msg')].map(e => e.textContent)"
EARLIER = "#messages .earlier button"


def test_earlier_messages_load_above_and_stay_while_new_ones_arrive(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.messages, api.start = said("new", 30), 500
    api.pages = {"before=500": {"messages": said("mid", 20), "start": 200}, "before=200": {"messages": said("old", 2), "start": 0}}
    open_first(chrome, url)
    chrome.wait(f"document.querySelector('{EARLIER}')?.textContent === 'Load earlier'")
    chrome.js("document.querySelector('#messages').scrollTop = 0")
    top = "document.querySelector('#messages .live .msg').getBoundingClientRect().top"
    before = chrome.js(top)
    chrome.click(EARLIER)
    chrome.wait("document.querySelectorAll('#messages .msg').length === 50")
    assert abs(chrome.js(top) - before) < 40
    api.messages = api.messages + said("later", 1)
    chrome.wait(f"{TEXTS}.at(-1) === 'later 0'")
    assert chrome.js(TEXTS)[:2] == ["mid 0", "mid 1"] and len(chrome.js(TEXTS)) == 51 and api.anchors[-1] == "500"
    chrome.click(EARLIER)
    chrome.wait(f"{TEXTS}[0] === 'old 0'")
    chrome.wait(f"document.querySelector('{EARLIER}').textContent === 'Earlier conversations'")


def test_a_clear_drops_the_older_messages_of_the_previous_conversation(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.start, api.pages = 500, {"before=500": {"messages": said("old", 2), "start": 0}}
    open_first(chrome, url)
    chrome.click(EARLIER)
    chrome.wait("document.querySelectorAll('#messages .msg').length === 3")
    api.messages, api.start, api.conversation = said("fresh", 1), 0, "two"
    chrome.wait(f"{TEXTS}.join('|') === 'fresh 0'")
    assert api.anchors[-1] == ""


def test_an_earlier_conversation_opens_read_only(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.conversations = [{"id": "past", "ts": time.time() - 3600, "title": "Fix the build"}]
    api.pages = {"conversation=past": {"messages": said("then", 2), "start": 90}, "conversation=past&before=90": {"messages": said("first", 1), "start": 0}}
    open_first(chrome, url)
    chrome.click("#sMenu")
    chrome.click("#sheet [data-p]")
    chrome.wait("document.querySelector('#sheet .past-list button')?.textContent.includes('Fix the build')")
    chrome.click("#sheet .past-list button")
    chrome.wait("document.querySelectorAll('#sheet .past-chat .msg').length === 2")
    chrome.click("#sheet .past-more")
    chrome.wait("document.querySelector('#sheet .past-chat .msg').textContent === 'first 0' && !document.querySelector('#sheet .past-more')")
    assert chrome.js("document.querySelectorAll('#sheet textarea, #sheet input').length") == 0


def test_older_messages_are_dropped_rather_than_left_with_a_hole(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.start, api.pages = 500, {"before=500": {"messages": said("old", 2), "start": 100}}
    open_first(chrome, url)
    chrome.click(EARLIER)
    chrome.wait("document.querySelectorAll('#messages .msg').length === 3")
    api.moved = 900  # the server could not reach back to the anchor any more
    chrome.wait("document.querySelectorAll('#messages .msg').length === 1")
    chrome.wait(f"document.querySelector('{EARLIER}').textContent === 'Load earlier'")


# --- reading: markdown blocks and code copy ---
def open_reader(chrome, url):
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("typeof rich === 'function' && document.querySelector('.card') !== null")


def rendered(chrome, source, expression):
    """Draw `source` as an answer into a detached box and ask the box something."""
    return chrome.js(f"(() => {{ const d = document.createElement('div'); d.innerHTML = rich({json.dumps(source)}); return {expression}; }})()")


def texts(selector):
    return f"[...d.querySelectorAll({json.dumps(selector)})].map(e => e.textContent)"


def test_markdown_blocks_render(api_fixture):
    chrome, api, url = api_fixture
    open_reader(chrome, url)
    source = ("# One\n## Two\n### Three\n#### Four\n\nplain **bold** `x<y`\n\n- a\n- b\n  - inner\n      - deeper\n\n3. c\n4. d\n\n"
              "| h1 | h2 |\n|:---|---:|\n| 1 | 2 |\n\n```python\nif a < b:\n    pass\n```\n")
    assert rendered(chrome, source, texts("h3")) == ["One", "Two"]
    assert rendered(chrome, source, texts("h4")) == ["Three", "Four"]
    assert rendered(chrome, source, texts("p")) == ["plain bold x<y"]
    assert rendered(chrome, source, "d.querySelectorAll('ul > li').length") == 4
    assert rendered(chrome, source, texts("ul ul > li")) == ["inner", "deeper"]
    assert rendered(chrome, source, "d.querySelectorAll('ul ul ul').length") == 0
    assert rendered(chrome, source, "[d.querySelector('ol').getAttribute('start'), d.querySelectorAll('ol > li').length]") == ["3", 2]
    assert rendered(chrome, source, texts(".tbl > table th")) == ["h1", "h2"] and rendered(chrome, source, texts(".tbl td")) == ["1", "2"]
    assert rendered(chrome, source, "d.querySelector('.code .bar span').textContent") == "python"
    assert rendered(chrome, source, "d.querySelector('.code pre > code').textContent") == "if a < b:\n    pass"
    assert rendered(chrome, source, "d.querySelector('.code .bar button.copy').textContent") == "Copy"


def test_half_built_markdown_stays_visible(api_fixture):
    chrome, api, url = api_fixture
    open_reader(chrome, url)
    for source in ("| a | b |\nnext", "> quoted", "##### five"):
        assert rendered(chrome, source, "[d.textContent, d.querySelectorAll('table, h3, h4, blockquote').length]") == [source, 0], source
    assert rendered(chrome, "```\nleft open\n| a |", "d.querySelector('.code code').textContent") == "left open\n| a |"


def test_tables_survive_crlf_code_pipes_and_ragged_rows(api_fixture):
    chrome, api, url = api_fixture
    open_reader(chrome, url)
    source = "| cmd | why | n |\r\n|---|---|---|\r\n| `a|b` | x \\| y | 1 |\r\n| short |\r\n| p | q | r | s | t |\r\n"
    rows = "[...d.querySelectorAll('tbody tr')].map(r => [...r.cells].map(c => c.textContent))"
    assert rendered(chrome, source, rows) == [["a|b", "x | y", "1"], ["short", "", ""], ["p", "q", "r | s | t"]]


def test_markdown_never_lets_markup_through(api_fixture):
    chrome, api, url = api_fixture
    open_reader(chrome, url)
    hostile = ('<script>window.pwned=1</script>\n<img src=x onerror=window.pwned=1>\n[x](javascript:alert(1))\n![y](javascript:alert(2))\n'
               '# <img src=x onerror=window.pwned=1>\n| <script>a</script> |\n|---|\n| <img src=x onerror=window.pwned=1> |\n\n'
               '- <svg onload=window.pwned=1>\n```"><img src=x onerror=window.pwned=1>\ncode\n```\n\u00010\u0002 [`z`](a.md)')
    chrome.js(f"window.box = document.createElement('div'); document.body.appendChild(box); box.innerHTML = rich({json.dumps(hostile)})")
    time.sleep(0.2)
    assert chrome.js("window.pwned === undefined")
    assert chrome.js("box.querySelectorAll('script, img, svg, [onerror], [onload], a[href^=javascript]').length") == 0
    assert chrome.js("box.querySelector('a code').textContent") == "z"
    assert chrome.js("!/[\\u0001\\u0002]/.test(box.textContent)")


def test_a_code_span_inside_a_link_target_stays_out_of_the_href(api_fixture):
    chrome, api, url = api_fixture
    open_reader(chrome, url)
    hostile = '[x](https://a/`"onmouseover=window.pwned=1//`)'
    chrome.js(f"window.box = document.createElement('div'); document.body.appendChild(box); box.innerHTML = rich({json.dumps(hostile)})")
    assert chrome.js("box.querySelectorAll('[onmouseover]').length") == 0
    assert chrome.js("[...box.querySelectorAll('a')].every(a => !/code|[<>\\u0001\\u0002]/.test(a.getAttribute('href')))")
    assert chrome.js("!/[\\u0001\\u0002]/.test(box.textContent)")


def test_markdown_stays_fast_on_hostile_text(api_fixture):
    chrome, api, url = api_fixture
    open_reader(chrome, url)
    for source in ("'['.repeat(100000)", "'|'.repeat(100000) + '\\n|-|'", "'`'.repeat(100000)", "'a/'.repeat(100000)", "'- item\\n'.repeat(20000)",
                   "'a/' + '!'.repeat(100000)", "'http://' + '.'.repeat(100000)", "' '.repeat(100000) + 'x'", "'|' + ' '.repeat(100000) + '\\n|-' + ' '.repeat(100000) + 'x'", "'![]('.repeat(100000)"):
        for call in ("rich", "md", "imagePaths"):
            took = chrome.js(f"(() => {{ const s = {source}, a = performance.now(); {call}(s); return performance.now() - a; }})()")
            assert took < 1000, (call, source, took)


def test_user_and_screen_messages_stay_as_typed(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [{"role": "user", "text": "# mine"}, {"role": "assistant", "text": "# theirs"}]
    open_first(chrome, url)
    assert chrome.js("document.querySelector('.msg.user').textContent") == "# mine"
    assert chrome.js("document.querySelector('.msg.assistant.md h3').textContent") == "theirs"
    api.source, api.messages = "screen", [{"role": "assistant", "text": "# terminal"}]
    api.publish_session("working")
    chrome.wait("document.querySelector('.msg.assistant')?.textContent === '# terminal'")
    assert chrome.js("document.querySelector('.msg h3') === null")


def test_wide_tables_scroll_inside_their_box(api_fixture):
    chrome, api, url = api_fixture
    row = "|" + "|".join(f" column number {i} " for i in range(8)) + "|"
    api.messages = [{"role": "assistant", "text": "\n".join([row, "|" + "---|" * 8, row])}]
    chrome.resize(320, 700)
    open_first(chrome, url)
    assert chrome.js("document.documentElement.scrollWidth") <= 320
    assert chrome.js("(() => { const t = document.querySelector('.tbl'); return t.scrollWidth > t.clientWidth && t.getBoundingClientRect().right <= document.querySelector('#messages').getBoundingClientRect().right; })()")
    assert chrome.js("getComputedStyle(document.querySelector('.tbl td')).position") == "sticky"


def test_copy_puts_raw_code_on_the_clipboard(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [{"role": "assistant", "text": "```sh\necho \"<a&b>\"\n  two\n```"}]
    open_first(chrome, url)
    chrome.js("Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: t => (window.copied = t, Promise.resolve())}})")
    assert chrome.js("document.querySelector('button.copy').getBoundingClientRect().height") >= 44
    chrome.click("button.copy")
    chrome.wait("document.querySelector('button.copy').textContent === 'Copied'")
    assert chrome.js("window.copied") == 'echo "<a&b>"\n  two'
    chrome.js("Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText: () => Promise.reject(new Error('no'))}})")
    chrome.click("button.copy")
    chrome.wait("document.querySelector('#toasts .err')?.textContent === 'Could not copy'")


# --- reading: folded tool work, failures and refusals, keyed drawing ---
def tool(id, name, text, state="ok", **more):
    return {"role": "tool", "tool": name, "text": text, "ts": "t", "id": id, "state": state, **more}


LIVE = "[...document.querySelectorAll('#messages .live > *')].map(e => e.className)"
SUMS = "[...document.querySelectorAll('#messages .run .sum')].map(e => e.textContent)"
HIDDEN = "!document.querySelector('#messages .run .step').checkVisibility({visibilityProperty: true})"
SAY = {"role": "assistant", "text": "Looking.", "ts": "a"}


def test_successful_calls_fold_into_one_line(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [SAY, tool("r1", "Read", "src/" + "a_very_long_directory_name/" * 8 + "billing.py"), tool("r2", "Read", "b.py", result="x = 1"), tool("g1", "Grep", "retry")]
    chrome.resize(320, 700)
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .run') !== null")
    assert chrome.js(LIVE) == ["msg assistant md", "run"]
    assert chrome.js(SUMS) == ["2 reads, 1 search"]
    assert chrome.js("document.querySelector('#messages .run > button').getAttribute('aria-expanded')") == "false"
    assert chrome.js(HIDDEN)
    chrome.click("#messages .run > button")
    chrome.wait("document.querySelector('#messages .run > button').getAttribute('aria-expanded') === 'true'")
    wait_for(lambda: not chrome.js(HIDDEN), "the steps show")
    assert chrome.js("[...document.querySelectorAll('#messages .run .step b')].map(e => e.textContent)") == ["Read", "Read", "Grep"]
    assert "x = 1" in chrome.js("document.querySelectorAll('#messages .run .step')[1].textContent")
    assert chrome.js("document.documentElement.scrollWidth") <= 320
    assert chrome.js("document.querySelector('#messages .run').getBoundingClientRect().right <= document.querySelector('#messages').getBoundingClientRect().right")
    chrome.click("#messages .run > button")
    wait_for(lambda: chrome.js(HIDDEN), "the steps fold away again")
    api.messages = [SAY, tool("b1", "Bash", "ls"), tool("x1", "mcp__lala__durum", "")]
    chrome.wait(f"{SUMS}.join() === '1 command, 1 other job'")
    chrome.js("localStorage.setItem('lang', 'tr')")
    api.messages = [SAY, tool("r1", "Read", "a.py"), tool("r2", "Read", "b.py"), tool("g1", "Grep", "retry"), tool("w1", "WebFetch", "example.com")]
    open_first(chrome, url)
    chrome.wait(f"{SUMS}.join() === '2 okuma, 1 arama, 1 sayfa'")


def test_edit_runs_total_their_lines_and_show_the_diff(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [
        SAY,
        tool("e1", "Edit", "core.py", added=10, removed=2, diff=[[" ", "a"], ["-", "<b>x</b>"], ["+", "y"], ["…", ""], [" ", "z"]], diff_cut=True),
        tool("e2", "Edit", "app.py", added=3, removed=1, diff=[["-", "p"], ["+", "q"]]),
        tool("e3", "Edit", "old.py", state="unknown", added=50, removed=50, diff=[["+", "n"]]),
        tool("w1", "Write", "new.py", lines=5),
        tool("b1", "Bash", "pytest -q"),
    ]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .run') !== null")
    assert chrome.js(SUMS) == ["4 edits, 1 command · +13 −3"]
    chrome.click("#messages .run > button")
    step = lambda i, ask: chrome.js(f"(() => {{ const s = document.querySelectorAll('#messages .run .step')[{i}]; return {ask}; }})()")
    assert step(0, "[...s.querySelectorAll('.diff > *')].map(e => e.className + ':' + e.textContent)") == [":a", "d:<b>x</b>", "a:y", "gap:…", ":z"]
    assert step(0, "s.querySelector('.diff b')") is None
    assert "Diff cut short" in step(0, "s.textContent") and "Diff cut short" not in step(1, "s.textContent")
    assert step(0, "s.querySelector('.cnt').textContent") == "+10 −2"
    assert "5 lines written" in step(3, "s.textContent") and step(3, "s.querySelector('.diff')") is None


def test_failures_and_refusals_stay_open_and_split_the_run(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [
        SAY, tool("r1", "Read", "a.py"),
        tool("b1", "Bash", "pytest -q", state="failed", result="E assert 1 == 2\n1 failed"),
        tool("e1", "Edit", "a.py", added=1, removed=1),
        tool("b2", "Bash", "git push", state="rejected", result="use a <i>branch</i>"),
        tool("b3", "Bash", "rm -rf build", state="rejected"),
    ]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .act') !== null")
    assert chrome.js(LIVE) == ["msg assistant md", "run", "act bad", "run", "act denied", "act denied"]
    assert chrome.js("document.querySelector('.act.bad .out').checkVisibility({visibilityProperty: true})")
    bad = chrome.js("document.querySelector('.act.bad').textContent")
    assert "Failed" in bad and "Bash" in bad and "pytest -q" in bad and "1 failed" in bad
    first, second = chrome.js("[...document.querySelectorAll('.act.denied')].map(e => e.textContent)")
    assert "You said no" in first and "git push" in first and "use a <i>branch</i>" in first
    assert "It did not run." in second and "rm -rf build" in second
    assert chrome.js("document.querySelector('.act i')") is None
    token = lambda name: chrome.js(f"(() => {{ const p = document.createElement('i'); p.style.color = 'var(--{name})'; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; }})()")
    border = lambda q: chrome.js(f"(() => {{ const s = getComputedStyle(document.querySelector('{q}')); return [s.borderTopStyle, s.borderTopColor]; }})()")
    assert border(".act.bad") == ["solid", token("lal")] and border(".act.denied") == ["dashed", token("muted")]


def test_a_failure_of_many_short_lines_scrolls_inside_its_card(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [SAY, tool("b1", "Bash", "pytest -q", state="failed", result="E\n" * 300)]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .act.bad .out') !== null")
    height, inner, overflow = chrome.js("(() => { const o = document.querySelector('.act.bad .out'); return [o.clientHeight, o.scrollHeight, getComputedStyle(o).overflowY]; })()")
    assert height <= 320 and inner > height and overflow == "auto"


def test_the_running_step_is_named_on_the_summary(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [SAY, tool("r1", "Read", "a.py", state="unknown"), tool("b1", "Bash", "pytest -q\n  --maxfail=1", state="running")]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .run') !== null")
    assert chrome.js(SUMS) == ["1 read · Running: Bash pytest -q"]
    chrome.click("#messages .run > button")
    notes = chrome.js("[...document.querySelectorAll('#messages .run .step')].map(e => e.textContent.includes('No result in the record'))")
    assert notes == [True, False]
    api.messages = [SAY, tool("b1", "Bash", "pytest -q", state="running")]
    chrome.wait(f"{SUMS}.join() === 'Running: Bash pytest -q'")


def test_an_open_run_stays_open_while_the_conversation_moves(api_fixture):
    chrome, api, url = api_fixture
    r1, r2, r3 = tool("r1", "Read", "a.py"), tool("r2", "Read", "b.py", state="running"), tool("r3", "Grep", "x")
    api.messages = said("old", 1) + [SAY, r1, r2]
    open_first(chrome, url)
    chrome.click("#messages .run > button")
    chrome.js("window.r = document.querySelector('#messages .run')")
    api.messages = [SAY, r1, {**r2, "state": "ok"}, r3] + said("later", 1)
    chrome.wait(f"{TEXTS}.at(-1) === 'later 0'")
    assert chrome.js("document.querySelector('#messages .run') === window.r && window.r.classList.contains('open')")
    assert chrome.js("window.r.querySelectorAll('.step').length") == 3 and chrome.js(SUMS) == ["2 reads, 1 search"]
    assert chrome.js("window.r.querySelector('button').getAttribute('aria-expanded')") == "true"
    api.messages = [SAY, r1, {**r2, "state": "failed", "result": "No such file"}, r3] + said("later", 1)
    chrome.wait("document.querySelector('#messages .act.bad') !== null")
    assert chrome.js(LIVE) == ["msg assistant md", "run open", "act bad", "run open", "msg assistant md"]
    assert chrome.js("document.querySelector('#messages .run') === window.r && window.r.querySelectorAll('.step').length === 1")


def test_reading_position_holds_when_the_window_slides(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.messages = [{"role": "assistant", "text": f"line {i}\nsecond\nthird", "ts": f"l{i}"} for i in range(60)]
    open_first(chrome, url)
    chrome.wait("document.querySelectorAll('#messages .msg').length === 60")
    chrome.js("(() => { const b = document.querySelector('#messages'); b.scrollTop = b.scrollHeight / 2; })()")
    chrome.js("(() => { const b = document.querySelector('#messages'), top = b.getBoundingClientRect().top; window.a = [...b.querySelectorAll('.msg')].find(e => e.getBoundingClientRect().top > top + 10); })()")
    top = "window.a.getBoundingClientRect().top"
    before = chrome.js(top)
    api.messages = api.messages[5:] + [{"role": "assistant", "text": f"more {i}\nsecond\nthird\nfourth", "ts": f"m{i}"} for i in range(5)]
    chrome.wait(f"{TEXTS}.at(-1).startsWith('more 4')")
    assert chrome.js("window.a.isConnected") and abs(chrome.js(top) - before) <= 2
    assert chrome.js("document.querySelectorAll('#messages .msg').length") == 60
    assert chrome.js("getComputedStyle(document.querySelector('#messages')).overflowAnchor") == "none"


@pytest.mark.parametrize("source", ["transcript", "screen"])
def test_twin_messages_stay_two(api_fixture, source):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.source = source
    twin = {"role": "assistant", "text": "Done."} | ({"ts": "same"} if source == "transcript" else {})
    api.messages = [{"role": "user", "text": "go"}, twin, dict(twin)]
    open_first(chrome, url)
    chrome.wait("document.querySelectorAll('#messages .msg.assistant').length === 2")
    chrome.js("window.twins = [...document.querySelectorAll('#messages .msg.assistant')]")
    api.messages = [twin, dict(twin), {"role": "user", "text": "next"}]
    chrome.wait(f"{TEXTS}.at(-1) === 'next'")
    assert chrome.js(TEXTS) == ["Done.", "Done.", "next"]
    assert chrome.js("[...document.querySelectorAll('#messages .msg.assistant')].every((e, i) => e === window.twins[i])")
    api.messages = [dict(twin), {"role": "user", "text": "next"}]
    chrome.wait(f"{TEXTS}.length === 2")
    assert chrome.js(TEXTS) == ["Done.", "next"]


def test_a_state_change_alone_redraws_the_call(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [SAY, tool("b1", "Bash", "pytest -q", state="running"), {"role": "assistant", "text": "Waiting.", "ts": "b"}]
    open_first(chrome, url)
    chrome.wait(f"{SUMS}.join() === 'Running: Bash pytest -q'")
    api.messages = [SAY, tool("b1", "Bash", "pytest -q", state="failed", result="1 failed"), api.messages[2]]
    chrome.wait("document.querySelector('#messages .act.bad')?.textContent.includes('1 failed')")
    assert chrome.js(LIVE) == ["msg assistant md", "act bad", "msg assistant md"]
    api.messages = [SAY, tool("b1", "Bash", "pytest -q", result="2 passed"), api.messages[2]]
    chrome.wait(f"{SUMS}.join() === '1 command'")


def test_history_pages_group_too(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    page = [tool("h1", "Read", "a.py"), tool("h2", "Read", "b.py", state="failed", result="gone"), tool("h3", "Read", "c.py")] + said("mid", 1)
    api.start, api.pages = 500, {"before=500": {"messages": page, "start": 0}, "conversation=past": {"messages": page, "start": 0}}
    api.conversations = [{"id": "past", "ts": time.time() - 3600, "title": "Fix the build"}]
    open_first(chrome, url)
    chrome.click(EARLIER)
    chrome.wait("document.querySelectorAll('#messages .past .run').length === 2")
    assert chrome.js("[...document.querySelectorAll('#messages .past > *')].map(e => e.className)") == ["run", "act bad", "run", "msg assistant md"]
    chrome.click("#messages .past .run > button")
    api.messages = api.messages + said("later", 1)
    chrome.wait(f"{TEXTS}.at(-1) === 'later 0'")
    assert chrome.js("document.querySelector('#messages .past .run').classList.contains('open')")
    chrome.click("#sMenu")
    chrome.click("#sheet [data-p]")
    chrome.click("#sheet .past-list button")
    chrome.wait("document.querySelectorAll('#sheet .past-chat .run').length === 2")
    chrome.click("#sheet .past-chat .act.bad + .run > button")
    assert chrome.js("document.querySelector('#sheet .past-chat .act.bad + .run').classList.contains('open')")
    assert chrome.js("document.querySelector('#sheet .past-chat .act.bad .out').textContent") == "gone"


def test_fold_has_no_motion_when_reduced(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [SAY, tool("r1", "Read", "a.py")]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .run') !== null")
    duration = "getComputedStyle(document.querySelector('#messages .run .fold')).transitionDuration"
    assert chrome.js(duration).startswith("0.24s")
    assert chrome.js("document.querySelector('#messages .run > button').getBoundingClientRect().height") >= 44
    chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
    assert set(chrome.js(duration).split(", ")) == {"0s"}
    assert set(chrome.js("getComputedStyle(document.querySelector('#messages .run .chev')).transitionDuration").split(", ")) == {"0s"}


# --- reading: context, cache and limit above the conversation ---
META = "document.querySelector('#sMeta').textContent"
SHOWN = "!document.querySelector('#sMeta').hidden"
RING = "[document.querySelector('#sRing b')?.textContent, document.querySelector('#sRing small')?.textContent]"


def meters(tokens=124_300, ttl=3600, age=19 * 60 - 30, shift=0):
    now = time.time() + shift
    return {"context": {"tokens": tokens, "ts": now - 120}, "cache": {"ttl": ttl, "since": now - age}}


def limit(key, *windows, updated=0):
    return {"id": key, "label": key.title(), "updated": time.time() - updated, "items": [{"label": label, "used": used, "resets_at": resets} for label, used, resets in windows]}


def test_the_header_says_context_and_cache(api_fixture):
    chrome, api, url = api_fixture
    api.meters = meters()
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js(META) == "Context 124k · Cache ~41m" and "%" not in chrome.js(META)
    assert chrome.js("[999, 1000, 124300, 999600, 1240000].map(fmtTokens)") == ["999", "1k", "124k", "1M", "1.2M"]
    chrome.js("localStorage.setItem('lang', 'tr')")
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js(META) == "Bağlam 124k · Cache ~41 dk"


def test_cache_time_follows_the_computers_clock(api_fixture):
    chrome, api, url = api_fixture
    api.clock_shift = -7200
    api.meters = meters(age=30, shift=-7200)
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js(META) == "Context 124k · Cache ~59m"


def test_a_cold_cache_says_so(api_fixture):
    chrome, api, url = api_fixture
    api.meters = meters(ttl=300, age=400)
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js(META) == "Context 124k · Cache cold"


def test_unknown_measures_are_absent_not_zero(api_fixture):
    chrome, api, url = api_fixture
    api.meters = {"context": meters()["context"]}
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js(META) == "Context 124k"
    # with no limit ring beside it, this line is the only way to the meters sheet: a full touch target
    assert chrome.js("parseFloat(getComputedStyle(document.querySelector('#sMeta'), '::before').height)") >= 44
    api.meters = {"cache": meters()["cache"]}
    chrome.wait(f"{META} === 'Cache ~41m'")
    api.meters = {}
    chrome.wait("document.querySelector('#sMeta').hidden")
    assert chrome.js("document.querySelector('#sRing').hidden")
    assert chrome.js("getComputedStyle(document.querySelector('#sMeta')).display") == "none"


AGENTS = "document.querySelector('#sMeta [data-agents]')?.textContent"


def test_the_header_counts_the_agents_still_out(api_fixture):
    chrome, api, url = api_fixture
    api.meters = {**meters(), "agents": {"running": 2}}
    chrome.resize(320, 700)
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js(META) == "2 subagents running · Context 124k · Cache ~41m"
    # at 320 px the line is cut at its end; the count is what must stay whole
    assert chrome.js("(() => { const a = document.querySelector('#sMeta [data-agents]').getBoundingClientRect(), m = document.querySelector('#sMeta').getBoundingClientRect(); return a.width > 0 && a.right <= m.right + 0.5; })()")
    assert chrome.js("document.documentElement.scrollWidth") <= 320
    api.meters = {"agents": {"running": 1}}
    chrome.wait(f"{META} === '1 subagent running'")
    api.meters = meters()
    chrome.wait(f"{META} === 'Context 124k · Cache ~41m'")
    assert chrome.js(AGENTS) is None
    for unknown in ({"running": 0}, {"running": "2"}, {"running": 1.5}, {}, None):  # a number nobody counted is not shown
        api.meters = {"agents": unknown}
        chrome.wait("document.querySelector('#sMeta').hidden")
        assert chrome.js(AGENTS) is None
    chrome.js("localStorage.setItem('lang', 'tr')")
    api.meters = {**meters(), "agents": {"running": 2}}
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js(META) == "2 alt ajan çalışıyor · Bağlam 124k · Cache ~41 dk"
    api.meters = {"agents": {"running": 1}}
    chrome.wait(f"{META} === '1 alt ajan çalışıyor'")


def test_tapping_the_agents_count_brings_the_last_one_still_out_into_view(api_fixture):
    chrome, api, url = api_fixture
    filler = [{"role": "assistant", "text": f"Paragraph {i}. " + "Words that take room. " * 12} for i in range(8)]
    sub = lambda text, state: {"role": "tool", "tool": "Agent", "text": text, "agent": {"kind": "Explore", "state": state}}
    api.messages = [sub("First one out", "running"), *filler[:3], sub("Last one out", "running"), sub("Back already", "done"), *filler[3:]]
    api.meters = {**meters(), "agents": {"running": 2}}
    chrome.resize(390, 700)
    open_first(chrome, url)
    chrome.wait(SHOWN)
    seen = "(() => { const d = [...document.querySelectorAll('#messages details.sub[data-state=running]')].at(-1), r = d.getBoundingClientRect(), b = document.querySelector('#messages').getBoundingClientRect(); return d.textContent.includes('Last one out') && r.top >= b.top && r.bottom <= b.bottom; })()"
    time.sleep(0.6)  # the screen slides in
    assert not chrome.js(seen)

    def tap(x, y):
        for kind in ("mousePressed", "mouseReleased"):
            chrome.cdp("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    ax, ay, mx = chrome.js("(() => { const a = document.querySelector('#sMeta [data-agents]').getBoundingClientRect(), m = document.querySelector('#sMeta').getBoundingClientRect(); return [a.left + a.width / 2, a.top - 10, m.right - 4]; })()")
    tap(ax, ay)  # above the words: the whole height of the line answers, not only its 12 px letters
    chrome.wait(seen)
    assert chrome.js("document.querySelector('#sheet .meter') === null")
    tap(mx, ay)  # the measures beside it still open their sheet
    chrome.wait("document.querySelectorAll('#sheet .meter').length === 2")
    chrome.js("hideSheet()")
    # the card is gone from what is loaded: the tap does nothing rather than open the wrong thing
    api.messages = filler
    chrome.wait("document.querySelector('#messages details.sub') === null")
    tap(ax, ay)
    time.sleep(0.3)
    assert chrome.js("document.querySelector('#sheet .meter') === null")


def test_a_window_kept_for_one_model_shows_apart_and_counts_only_for_that_model(api_fixture):
    chrome, api, url = api_fixture
    fable = {"label": "Fable · 7d", "used": 88, "resets_at": time.time() + 3 * 86400, "model": "fable"}
    api.limits = [limit("claude", ("5h", 31, None), ("7d", 57, None))]
    api.limits[0]["items"].append(fable)
    chrome.cdp("Page.navigate", url=url)
    # the strip names the model, because "7d 88%" would read as the whole account's week
    chrome.wait("document.querySelector('#limits .lim em')?.textContent === 'Fable 88%'")
    assert chrome.js("document.querySelector('#limits').scrollWidth <= document.querySelector('#limits').clientWidth")
    chrome.click("#limits")
    chrome.wait("document.querySelectorAll('#sheet .li').length === 3")
    assert chrome.js("[...document.querySelectorAll('#sheet .li .lt span:first-child')].map(e => e.textContent)") == ["5h", "7d", "Fable · 7d"]
    assert "88% used" in chrome.js("document.querySelectorAll('#sheet .li')[2].textContent")
    # the fixture session runs Sonnet: the Fable week is not its limit
    open_first(chrome, url)
    chrome.wait(f"{RING}.join() === '57,wk'")
    chrome.click("#sRing")
    chrome.wait("document.querySelectorAll('#sheet .meter').length === 2")
    api.cards = [card("blocked", pending=APPROVAL, model="Fable 5.2")]
    open_first(chrome, url)
    chrome.wait(f"{RING}.join() === '88,Fable'")
    assert chrome.js("document.querySelector('#sRing').getAttribute('aria-label')") == "Claude Fable · 7d: 88% used"
    assert chrome.js("(() => { const r = document.querySelector('#sRing').getBoundingClientRect(), s = document.querySelector('#sRing small').getBoundingClientRect(); return s.left >= r.left - 0.5 && s.right <= r.right + 0.5; })()")
    chrome.click("#sRing")
    chrome.wait("document.querySelectorAll('#sheet .meter').length === 3")


def test_the_ring_carries_the_fullest_live_window_of_this_agent(api_fixture):
    chrome, api, url = api_fixture
    soon = time.time() + 3600
    api.limits = [limit("claude", ("5h", 93, soon), ("7d", 57, soon)), limit("codex", ("5h", 99, None))]
    open_first(chrome, url)
    chrome.wait("!document.querySelector('#sRing').hidden")
    assert chrome.js(RING) == ["93", "5h"]
    assert chrome.js("(() => { const r = document.querySelector('#sRing').getBoundingClientRect(); return r.width >= 44 && r.height >= 44; })()")
    assert chrome.js("document.querySelector('#sRing').getAttribute('aria-label')") == "Claude 5h: 93% used"
    assert not chrome.js("document.querySelector('#sRing').classList.contains('stale')")
    api.limits = [limit("claude", ("5h", 93, time.time() - 60), ("7d", 57, soon), updated=3600)]
    chrome.js("loadLimits()")
    chrome.wait(f"{RING}.join() === '57,wk'")
    assert chrome.js("document.querySelector('#sRing').classList.contains('stale')")
    api.limits = [limit("claude", ("7d", 94, None))]
    chrome.js("loadLimits()")
    chrome.wait(f"{RING}.join() === '94,wk'")
    token = chrome.js("(() => { const p = document.createElement('i'); p.style.color = 'var(--zer)'; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; })()")
    assert chrome.js("getComputedStyle(document.querySelector('#sRing .fill')).stroke") == token
    assert chrome.js("document.querySelector('#sRing .fill').getAttribute('stroke-dasharray')") == "94 100"
    api.limits = [limit("codex", ("5h", 99, None)), limit("claude", ("monthly", 12, time.time() - 5))]
    chrome.js("loadLimits()")
    chrome.wait("document.querySelector('#sRing').hidden")
    chrome.js("localStorage.setItem('lang', 'tr')")
    api.limits = [limit("claude", ("5h", 20, None), ("7d", 10, None))]
    open_first(chrome, url)
    chrome.wait(f"{RING}.join() === '20,5s'")


def test_the_meter_sheet_names_source_and_age(api_fixture):
    chrome, api, url = api_fixture
    api.meters = meters()
    api.limits = [limit("claude", ("5h", 93, time.time() + 3600), ("7d", 57, None), updated=3600)]
    open_first(chrome, url)
    chrome.wait(SHOWN + " && !document.querySelector('#sRing').hidden")
    rows = "[...document.querySelectorAll('#sheet .meter')].map(e => e.textContent)"
    for opener in ("#sMeta", "#sRing"):
        chrome.click(opener)
        chrome.wait("document.querySelectorAll('#sheet .meter').length === 4")
        context, cache, five, week = chrome.js(rows)
        assert "Context" in context and "124,300 tokens" in context and "from the session record, 2m ago" in context
        assert "~41m left" in cache and "1 hour" in cache and "from the session record" in cache and "ago" in cache
        assert "5h" in five and "93% used" in five and "account-wide, from your limits file" in five and "1h 0m ago" in five
        assert "7d" in week and "57% used" in week
        assert chrome.js("[...document.querySelectorAll('#sheet .meter')].map(e => !!e.querySelector('.bar'))") == [False, False, True, True]
        chrome.js("hideSheet()")
        chrome.wait("document.querySelector('#sheet .meter') === null")
    api.meters, api.limits = {"context": meters()["context"]}, [limit("codex", ("5h", 9, None))]
    api.cards = [card("idle", agent="codex")]
    chrome.js("loadLimits()")
    chrome.wait(f"{RING}.join() === '9,5h' && {META} === 'Context 124k'")
    chrome.click("#sRing")
    chrome.wait("document.querySelectorAll('#sheet .meter').length === 2")
    assert "account-wide, from Codex records or your limits file" in chrome.js(rows)[1]


def test_meters_do_not_leak_into_the_next_session(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.meters = meters()
    api.cards = [card("idle", project="a-project-with-a-very-long-name-indeed")]
    chrome.resize(320, 700)
    open_first(chrome, url)
    chrome.wait(SHOWN)
    assert chrome.js("document.documentElement.scrollWidth") <= 320
    assert chrome.js("document.querySelector('#session .top').scrollWidth <= document.querySelector('#session .top').clientWidth")
    reach = chrome.js("(() => { const m = document.querySelector('#sMeta'), r = m.getBoundingClientRect(); let n = 0; for (let y = Math.floor(r.top) - 12; y < r.bottom + 16; y++) if (document.elementFromPoint(r.left + 6, y)?.closest('#sMeta')) n++; return n; })()")
    assert reach >= 24
    api.fail_chat = True
    chrome.click("#back")
    chrome.wait("document.querySelector('#session').hidden")
    chrome.click(".card-open")
    chrome.wait("!document.querySelector('#session').hidden")
    assert chrome.js("document.querySelector('#sMeta').hidden")


# --- reading: the pictures a conversation points at ---
SRCS = "[...document.querySelectorAll('#messages .shot img')].map(e => e.getAttribute('src'))"
LOADED = "[...document.querySelectorAll('#messages .shot img')].every(e => e.complete && e.naturalWidth > 0)"
FILE = "/api/sessions/fake%3Ap1/file?path="


def test_pictures_named_in_a_message_show_under_it(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [
        {"role": "assistant", "text": "Saved /tmp/out/full.png and docs/shot-2.JPG.\n\n![the chart](chart.webp)", "ts": "a"},
        {"role": "user", "text": "compare with old/before.gif", "ts": "b"},
    ]
    open_first(chrome, url)
    chrome.wait("document.querySelectorAll('#messages .shot img').length === 4")
    assert chrome.js(SRCS) == [FILE + p for p in ("%2Ftmp%2Fout%2Ffull.png", "docs%2Fshot-2.JPG", "chart.webp", "old%2Fbefore.gif")]
    assert chrome.js("[...document.querySelectorAll('#messages .shot small')].map(e => e.textContent)") == ["full.png", "shot-2.JPG", "chart.webp", "before.gif"]
    assert chrome.js("[...document.querySelectorAll('#messages .msg')].map(e => e.querySelectorAll(':scope > .shots .shot').length)") == [3, 1]
    assert chrome.js("[...document.querySelectorAll('#messages .msg.assistant a')].map(e => e.textContent)") == ["/tmp/out/full.png", "docs/shot-2.JPG", "the chart"]
    chrome.wait(LOADED)
    assert chrome.js("[...document.querySelectorAll('#messages .shot img')].every(e => e.loading === 'lazy' && e.getBoundingClientRect().height === 160 && getComputedStyle(e).objectFit === 'contain')")
    assert chrome.js("document.querySelector('#messages .shot').getAttribute('aria-label')") == "Open picture full.png"


def test_code_remote_and_extra_pictures_are_left_as_text(api_fixture):
    chrome, api, url = api_fixture
    text = "see a/1.png, a/1.png and https://x.invalid/r.png //cdn.invalid/s.png file:///etc/t.png\n```\n/tmp/in-code.png\n```\na/2.png a/3.png a/4.png a/5.png a/6.png\n```\nb/open.png"
    api.messages = [
        {"role": "assistant", "text": text, "ts": "a"},
        {"role": "tool", "tool": "Read", "text": "shots/x.png"},
        tool("r1", "Read", "shots/y.png", result="shots/z.png"), tool("b1", "Bash", "open shots/w.png", state="failed", result="shots/v.png"),
    ]
    open_first(chrome, url)
    chrome.wait("document.querySelectorAll('#messages .shot img').length > 0")
    assert chrome.js(SRCS) == [FILE + f"a%2F{n}.png" for n in (1, 2, 3, 4)]
    assert chrome.js("document.querySelectorAll('#messages img').length") == 4
    assert chrome.js("imagePaths('x/a.png '.repeat(50000) + '[](' + '('.repeat(50000)).join()") == "x/a.png"
    time.sleep(0.5)
    assert set(api.files) <= {f"a/{n}.png" for n in (1, 2, 3, 4)}


def test_a_picture_that_will_not_load_leaves_the_link(api_fixture):
    chrome, api, url = api_fixture
    api.missing = {"gone/x.png"}
    api.messages = [{"role": "assistant", "text": "Look at gone/x.png", "ts": "a"}]
    open_first(chrome, url)
    wait_for(lambda: "gone/x.png" in api.files, "the picture is asked for")
    chrome.wait("document.querySelector('#messages .shot') === null && document.querySelector('#messages .shots') === null")
    assert chrome.js("document.querySelector('#messages .msg a').textContent") == "gone/x.png"


def test_pictures_are_not_fetched_again_on_redraw(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [{"role": "assistant", "text": "Look at a/1.png", "ts": "a"}]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .shot img') !== null && " + LOADED)
    chrome.js("window.shot = document.querySelector('#messages .shot img')")
    api.messages = api.messages + said("later", 1)
    chrome.wait(f"{TEXTS}.at(-1) === 'later 0'")
    api.messages = api.messages + said("again", 1)
    chrome.wait(f"{TEXTS}.at(-1) === 'again 0'")
    assert chrome.js("document.querySelector('#messages .shot img') === window.shot")
    assert api.files == ["a/1.png"]


def test_the_viewer_closes_by_button_and_back_and_returns_focus(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.messages = said("before", 30) + [{"role": "assistant", "text": "Look at a/1.png", "ts": "a"}] + said("after", 3)
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .shot img') !== null")
    place = "document.querySelector('#messages').scrollTop"
    opened = "!document.querySelector('#viewer').hidden"
    closers = ("document.querySelector('#viewerClose').click()", "history.back()", "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))", "document.querySelector('#viewer').click()")
    for close in closers:
        before = chrome.js(place)
        chrome.click("#messages .shot")
        chrome.wait(opened)
        assert chrome.js("document.querySelector('#viewer img').getAttribute('src')") == FILE + "a%2F1.png"
        assert chrome.js("history.state.viewer === true && document.activeElement.id === 'viewerClose' && document.querySelector('main').inert")
        assert chrome.js("(() => { const r = document.querySelector('#viewerClose').getBoundingClientRect(); return r.width >= 44 && r.height >= 44; })()")
        assert chrome.js("getComputedStyle(document.querySelector('#viewer')).animationName") == "none"
        chrome.js(close)
        chrome.wait("document.querySelector('#viewer').hidden && !history.state.viewer")
        assert chrome.js("document.activeElement === document.querySelector('#messages .shot') && !document.querySelector('main').inert")
        assert chrome.js("!document.querySelector('#session').hidden") and chrome.js(place) == before
    chrome.click("#messages .shot")
    chrome.wait(opened)
    chrome.js("history.go(-2)")
    chrome.wait("!document.querySelector('#home').hidden")
    assert chrome.js("document.querySelector('#viewer').hidden && !document.querySelector('main').inert")
    assert api.files == ["a/1.png"]


def test_a_picture_loading_does_not_move_the_text(api_fixture):
    chrome, api, url = api_fixture
    api.file_gate.clear()
    api.messages = [{"role": "assistant", "text": "Look at a/1.png", "ts": "a"}] + said("after", 2)
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages .shot img') !== null")
    below = "[...document.querySelectorAll('#messages .msg')].at(-1).offsetTop"
    assert not chrome.js("document.querySelector('#messages .shot img').complete")
    before = chrome.js(below)
    api.file_gate.set()
    chrome.wait(LOADED)
    assert chrome.js(below) == before


# --- Yayın 1b: notification health ---
def test_settings_say_whether_notifications_still_reach_this_phone(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    states = chrome.js("""[bellFrom('denied', true, {known: true}), bellFrom('granted', false, null), bellFrom('default', false, null),
        bellFrom('granted', true, {known: false}), bellFrom('granted', true, {known: true, ok_at: 10, fail_at: 20}),
        bellFrom('granted', true, {known: true, ok_at: 30, fail_at: 20}), bellFrom('granted', true, {known: true, ok_at: null, fail_at: 20}),
        bellFrom('granted', true, {known: true, ok_at: null, fail_at: null})]""")
    assert states == ["denied", "off", "off", "off", "stopped", "on", "stopped", "on"]
    open_settings(chrome)
    row = "document.querySelector('#sheet .rows').firstElementChild"
    chrome.js("bellEndpoint = 'https://push.example/mine'; bellHealth = {known: true, ok_at: Date.now() / 1000 - 185, fail_at: null}; setBell('on')")
    assert chrome.js(f"{row}.textContent") == "Notifications onreached this phone 3m ago"
    assert chrome.js("document.querySelector('#settings').classList.contains('dot')") is False
    chrome.js("bellHealth = {known: true, ok_at: null, fail_at: null}; setBell('on')")
    assert chrome.js(f"{row}.textContent") == "Notifications on"
    chrome.click("#sheet #bellTest")
    chrome.wait("document.querySelector('#toasts .ok')?.textContent === 'Test sent. It should arrive in a moment'")
    assert api.push_tests == [{"endpoint": "https://push.example/mine"}]
    api.push_test_reply = {"ok": False, "code": 403}
    chrome.click("#sheet #bellTest")
    chrome.wait("document.querySelector('#toasts .err')?.textContent === 'The push service refused the test (403)'")
    chrome.wait("document.querySelector('#sheet #bell') !== null")
    assert chrome.js("document.querySelector('#sheet #bell').textContent") == "Notifications stopped on this device. Turn them on again"
    assert chrome.js("document.querySelector('#sheet #bellTest') === null")
    assert chrome.js("document.querySelector('#settings').classList.contains('dot')") is True
    chrome.js("setBell('denied')")
    assert chrome.js(f"{row}.textContent") == "Notifications are turned off for Ulak in this device's settings"
    assert chrome.js("document.querySelector('#sheet #bell, #sheet #bellTest') === null")
    assert chrome.js("document.querySelector('#settings').classList.contains('dot')") is True
    chrome.js("LANG = 'tr'; bellHealth = {known: true, ok_at: Date.now() / 1000 - 185}; setBell('on')")
    assert chrome.js(f"{row}.textContent") == "Bildirimler açık3dk önce bu telefona ulaştı"
    assert chrome.js("document.querySelector('#sheet #bellTest').textContent") == "Deneme bildirimi gönder"
    assert chrome.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


# --- Yayın 1b: characters nobody can see ---
def test_an_approval_with_hidden_characters_warns_and_cannot_be_approved_from_the_list(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card [data-act=approve]') !== null")  # a plain short command is approved from the list
    assert chrome.js("document.querySelector('.hidden-note') === null")
    api.publish_prompt({**APPROVAL, "fingerprint": "hidden-chars", "details": "r⟨U+200B⟩m notes.txt", "hidden": ["U+200B", "U+202E"]})
    chrome.wait("document.querySelector('.card .c-cmd')?.textContent.includes('⟨U+200B⟩')")
    assert chrome.js("document.querySelector('.card [data-act=approve]') === null")
    assert chrome.js("document.querySelector('.card .hidden-note').textContent") == "Has characters you cannot see: U+200B, U+202E"
    chrome.click(".card-open")
    chrome.wait("document.querySelector('#promptZone .hidden-note') !== null")
    assert chrome.js("document.querySelector('#promptZone .hidden-note').textContent") == "This command has characters you cannot see: U+200B, U+202E. They are written out where they stand."
    assert chrome.js("document.querySelector('#promptZone pre').textContent") == "r⟨U+200B⟩m notes.txt"
    assert chrome.js("document.querySelector('#promptZone [data-d=approve]') !== null && document.querySelector('#promptZone [data-d=deny]') !== null")
    assert chrome.js("document.querySelector('#promptZone [data-d=always]') === null")
    note = chrome.js("(() => { const r = document.querySelector('#promptZone .hidden-note').getBoundingClientRect(); return [r.width > 0, r.right <= innerWidth]; })()")
    assert note == [True, True]


# --- Yayın 1b: what a photo says about where it was taken ---
PICK = """(async (name, type, real) => {
    let bytes = new Uint8Array([1, 2, 3, 4, 5]);
    if (real) { const c = document.createElement('canvas'); c.width = 40; c.height = 20; c.getContext('2d').fillRect(0, 0, 40, 20);
                bytes = new Uint8Array(await (await new Promise(done => c.toBlob(done, 'image/png'))).arrayBuffer()); }
    const dt = new DataTransfer(); dt.items.add(new File([bytes], name, {type}));
    const input = document.querySelector('#fileIn'); input.files = dt.files; input.dispatchEvent(new Event('change'));
})"""


def test_a_heic_photo_is_redrawn_before_it_leaves_the_phone_and_the_owner_hears_when_it_could_not_be(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    chrome.js(PICK + "('IMG_1.HEIC', 'image/heic', true)")  # a picture this browser can draw
    chrome.wait("document.querySelector('#message').value.includes('/tmp/up-1')")
    assert api.uploads[0] == ("image/jpeg", b"\xff\xd8\xff\xe0")
    assert chrome.js("[...document.querySelectorAll('#toasts .toast')].map(el => el.textContent)") == ["File attached. Add a note and send."]
    chrome.js(PICK + "('IMG_2.HEIC', 'image/heic', false)")  # one it cannot
    chrome.wait("document.querySelector('#message').value.includes('/tmp/up-2')")
    assert api.uploads[1] == ("image/heic", b"\x01\x02\x03\x04")
    chrome.wait("[...document.querySelectorAll('#toasts .toast')].some(el => el.textContent === 'File attached. Where and with what this photo was taken may still be in it.')")
    chrome.js(PICK + "('notes.txt', 'text/plain', false)")
    chrome.wait("document.querySelector('#message').value.includes('/tmp/up-3')")
    assert api.uploads[2] == ("text/plain", b"\x01\x02\x03\x04")
    chrome.js(PICK + "('shot.png', 'image/png', true)")
    chrome.wait("document.querySelector('#message').value.includes('/tmp/up-4')")
    assert api.uploads[3] == ("image/png", b"\x89PNG")


# --- Yayın 1b: the tab title counts, a stale limit does not shout ---
def test_the_tab_title_counts_what_is_waiting(api_fixture):
    chrome, api, url = api_fixture
    api.cards = [card("blocked", pending=APPROVAL), {**card("blocked", pending=APPROVAL), "pane_id": "fake:p2"}, {**card("working"), "pane_id": "fake:p3"}]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.title === '(2) Ulak'")
    chrome.js("toggleLang()")
    chrome.wait("document.documentElement.lang === 'tr'")
    assert chrome.js("document.title") == "(2) Ulak"
    api.publish_session("working")
    api.events.append({"type": "removed", "pane": "fake:p2"})
    chrome.wait("document.title === 'Ulak'")


def test_a_stale_limit_keeps_its_figure_and_loses_its_alarm(api_fixture):
    chrome, api, url = api_fixture
    now = time.time()
    api.limits = [{"label": "Claude", "updated": now - 1801, "items": [{"label": "7d", "used": 95, "resets": 0}]},
                  {"label": "Codex", "updated": now - 125, "items": [{"label": "7d", "used": 95, "resets": 0}]},
                  {"label": "agy", "items": [{"label": "7d", "used": 10, "resets": 0}]}]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('#limits .lim').length === 3")
    assert chrome.js("[...document.querySelectorAll('#limits .lim')].map(e => [e.classList.contains('stale'), e.querySelector('.lv').className, e.querySelector('em').textContent])") == [
        [True, "lv", "95%"], [False, "lv hot", "95%"], [False, "lv ok", "10%"]]
    chrome.click("#limits")
    chrome.wait("document.querySelectorAll('#sheet .lp').length === 3")
    assert chrome.js("[...document.querySelectorAll('#sheet .lp')].map(e => [e.querySelector('h4 small')?.textContent ?? null, e.querySelector('h4 small')?.className ?? null, e.querySelector('.lv').className])") == [
        ["not updated for 30m", "", "lv big"], ["read 2m ago", "fresh", "lv big hot"], [None, None, "lv big ok"]]
    stale, fresh = chrome.js("[...document.querySelectorAll('#sheet .lp h4 small')].map(e => getComputedStyle(e).color)")
    assert stale != fresh


# --- Yayın 1b: content security policy ---
def test_nothing_the_app_does_is_refused_by_its_own_content_policy(api_fixture):
    chrome, api, url = api_fixture
    now = time.time()
    api.limits = [{"label": "Claude", "updated": now, "items": [{"label": "7d", "used": 40, "resets": 0}]}]
    api.messages = [{"role": "assistant", "text": "Look:\n\n![shot](shot.png)\n\n```\ncode\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |"}]
    chrome.cdp("Page.enable")
    chrome.cdp("Page.addScriptToEvaluateOnNewDocument", source="window.__refused = []; document.addEventListener('securitypolicyviolation', e => __refused.push(`${e.violatedDirective} ${e.blockedURI}`));")
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null && document.querySelectorAll('#limits .lim').length === 1")
    assert chrome.js("parseFloat(getComputedStyle(document.querySelector('#limits .lv i')).width) > 5")  # widths set from script still apply
    chrome.click("#limits")
    chrome.wait("document.querySelector('#sheet .lp') !== null")
    chrome.js("hideSheet()")
    open_settings(chrome)
    chrome.js("hideSheet()")
    chrome.click(".card-open")
    chrome.wait("!document.querySelector('#session').hidden && document.querySelector('#messages .msg') !== null")
    chrome.wait("document.querySelector('#messages img')?.naturalWidth > 0")
    chrome.js(PICK + "('IMG_1.HEIC', 'image/heic', true)")  # the redraw reads a blob: picture
    chrome.wait("document.querySelector('#message').value.includes('/tmp/up-1')")
    assert api.uploads[0][0] == "image/jpeg"
    chrome.js("navigator.serviceWorker.getRegistration().then(r => { window.__worker = !!r; })")
    chrome.wait("window.__worker === true")
    assert chrome.js("__refused") == []
    chrome.js("const s = document.createElement('script'); s.textContent = 'window.__ran = 1'; document.head.appendChild(s); const i = new Image(); i.src = 'https://example.invalid/x.png'")
    chrome.wait("__refused.length === 2")
    assert chrome.js("window.__ran === undefined") and sorted(r.split()[0] for r in chrome.js("__refused")) == ["img-src", "script-src-elem"]


SW_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const handlers = {}, shown = [], opened = [];
let clients = [];
const self = {
  addEventListener: (name, fn) => { handlers[name] = fn; },
  skipWaiting() {},
  clients: { claim: async () => {}, matchAll: async () => clients, openWindow: async url => { opened.push(url); } },
  registration: { showNotification: async (title, options) => { shown.push({ title, ...options }); } },
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), { self });
const client = (visibilityState, focus) => {
  const c = { visibilityState, posted: [], focused: 0, postMessage(m) { c.posted.push(m); } };
  c.focus = async () => { if (focus === 'fails') throw new Error('not allowed'); c.focused++; return c; };
  return c;
};
const fire = async (name, event) => { let wait; handlers[name]({ ...event, waitUntil: p => { wait = p; } }); await wait; };
const push = data => fire('push', { data: { json: () => data } });
const tap = data => fire('notificationclick', { notification: { close() {}, data } });
(async () => {
  const out = {};
  const waiting = { pane: 'w1:p2', title: 'proj · waiting', body: 'rm -rf build', tag: 'ulak-abc', url: '/?pane=w1%3Ap2', status: 'blocked' };

  clients = [];
  await push(waiting);
  out.closed = shown.splice(0);

  const hidden = client('hidden');
  clients = [hidden];
  await push(waiting);
  out.hidden = { shown: shown.splice(0).length, posted: hidden.posted };

  const open = client('visible');
  clients = [client('hidden'), open];
  await push(waiting);
  out.visible = { shown: shown.splice(0).length, posted: open.posted };

  await push({ ...waiting, test: true });
  out.test = shown.splice(0).length;

  const back = client('hidden'), front = client('visible');
  clients = [back, front];
  await tap({ pane: 'w1:p2', url: '/?pane=w1%3Ap2' });
  out.tap = { back: [back.posted.length, back.focused], front: [front.posted, front.focused], opened: opened.splice(0) };

  const stuck = client('hidden', 'fails');
  clients = [stuck];
  await tap({ pane: 'w1:p2', url: '/?pane=w1%3Ap2' });
  out.stuck = opened.splice(0);

  clients = [];
  await tap({ pane: 'w1:p2', url: '/?pane=w1%3Ap2' });
  await tap({ pane: 'w1:p2', url: '/?pane=w1%3Ap2' });
  out.cold = opened.splice(0);
  console.log(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def service_worker(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to run the service worker outside a browser")
    harness = tmp_path_factory.mktemp("sw") / "harness.js"
    harness.write_text(SW_HARNESS)
    run = subprocess.run([node, str(harness), str(Path(WEB) / "sw.js")], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_service_worker_shows_a_push_when_no_window_is_visible(service_worker):
    assert len(service_worker["closed"]) == 1
    note = service_worker["closed"][0]
    assert (note["title"], note["body"], note["tag"]) == ("proj · waiting", "rm -rf build", "ulak-abc")
    assert note["data"] == {"pane": "w1:p2", "url": "/?pane=w1%3Ap2"}
    assert service_worker["hidden"] == {"shown": 1, "posted": []}


def test_service_worker_hands_a_push_to_the_visible_window_instead_of_the_lock_screen(service_worker):
    assert service_worker["visible"] == {"shown": 0, "posted": [{"type": "pushed", "pane": "w1:p2"}]}
    assert service_worker["test"] == 1  # the owner asked to see this one arrive


def test_tapping_a_notification_opens_the_session_in_the_window_already_in_front(service_worker):
    tap = service_worker["tap"]
    assert tap["front"] == [[{"type": "open", "pane": "w1:p2"}], 1]
    assert tap["back"] == [0, 0] and tap["opened"] == []


def test_tapping_a_notification_still_lands_on_the_session_when_the_window_cannot_be_focused(service_worker):
    assert service_worker["stuck"] == ["/?pane=w1%3Ap2"]
    assert service_worker["cold"] == ["/?pane=w1%3Ap2", "/?pane=w1%3Ap2"]


OTHER = "fake:p2"


def other_session(api, status, pending=None):
    row = card(status, pane_id=OTHER, project="other-project", pending=pending)
    api.cards = [api.cards[0], row]
    api.events.append({"type": "session", "session": row})


CALL = "document.querySelector('#toasts .call')"


def test_a_question_in_another_session_calls_out_above_the_open_chat_and_a_tap_goes_there(api_fixture):
    chrome, api, url = api_fixture
    chrome.resize(390, 844)
    open_first(chrome, url)
    other_session(api, "working")
    chrome.wait("document.title.length > 0")
    time.sleep(0.3)
    assert chrome.js(f"{CALL} === null")
    question = {**APPROVAL, "details": "git push --force origin main " + "x" * 300, "fingerprint": "other-approval"}
    other_session(api, "blocked", question)
    chrome.wait(f"{CALL} !== null")
    text = chrome.js(f"{CALL}.textContent")
    assert "other-project" in text and "git push --force origin main" in text
    assert chrome.js(f"{CALL}.tagName") == "BUTTON"
    box = chrome.js(f"(() => {{ const r = {CALL}.getBoundingClientRect(); return [r.left, r.right, r.height, document.documentElement.scrollWidth]; }})()")
    assert box[0] >= 0 and box[1] <= 390 and box[2] >= 44 and box[3] <= 390
    other_session(api, "blocked", question)  # the same question again does not stack a second call
    time.sleep(0.3)
    assert chrome.js("document.querySelectorAll('#toasts .call').length") == 1
    chrome.js(f"{CALL}.click()")
    chrome.wait("location.hash === '#/s/fake:p2'")
    chrome.wait(f"{CALL} === null")


def test_the_call_leaves_when_the_question_is_answered_elsewhere(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    other_session(api, "blocked", {**APPROVAL, "fingerprint": "other-approval"})
    chrome.wait(f"{CALL} !== null")
    other_session(api, "working")
    chrome.wait(f"{CALL} === null")


def test_no_call_on_the_list_or_for_the_session_already_open(api_fixture):
    chrome, api, url = api_fixture
    open_first(chrome, url)
    api.publish_session("working")
    chrome.wait("document.title === 'Ulak'")
    api.publish_prompt({**APPROVAL, "fingerprint": "second-approval"})
    chrome.wait("document.title === '(1) Ulak'")
    assert chrome.js(f"{CALL} === null")
    chrome.click("#back")
    chrome.wait("!document.querySelector('#home').hidden")
    other_session(api, "blocked", {**APPROVAL, "fingerprint": "other-approval"})
    chrome.wait("document.title === '(2) Ulak'")
    assert chrome.js(f"{CALL} === null")


# --- search ---

def hit(snippet, at, **fields):
    return {"offset": 700, "end": 780, "role": "assistant", "ts": "2026-11-28T08:00:00Z", "snippet": snippet, "at": at, "conversation": "", "title": "", "near": True, **fields}


def look_for(chrome, text):
    chrome.click("#sSearch")
    chrome.wait("document.querySelector('#sheet input[type=search]') !== null")
    chrome.js(f"(() => {{ const i = document.querySelector('#sheet input[type=search]'); i.value = {json.dumps(text)}; i.dispatchEvent(new Event('input', {{bubbles: true}})); }})()")


def test_search_button_shows_only_with_a_transcript(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.source = "screen"
    open_first(chrome, url)
    assert chrome.js("document.querySelector('#sSearch').hidden") is True
    api.source, api.messages = "transcript", said("now", 2)
    chrome.wait("!document.querySelector('#sSearch').hidden")
    box = chrome.js("(() => { const r = document.querySelector('#sSearch').getBoundingClientRect(); return [r.width, r.height]; })()")
    assert min(box) >= 44 and chrome.js("document.querySelector('#sSearch').getAttribute('aria-label')") == "Search"
    chrome.click("#sSearch")
    chrome.wait("document.activeElement?.type === 'search'")
    assert chrome.js("parseFloat(getComputedStyle(document.activeElement).fontSize)") >= 16
    assert chrome.js("[document.activeElement.maxLength, document.activeElement.enterKeyHint]") == [80, "search"]


def test_search_lists_hits_and_marks_the_match_as_text(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.search = {"hits": [hit("x <img src=x onerror=window.pwned=1> y", [2, 6]), hit("you said kiraz", [9, 14], role="user")], "partial": False}
    open_first(chrome, url)
    look_for(chrome, "a")
    time.sleep(0.6)
    assert api.searches == []   # one letter is not asked for
    look_for(chrome, " <img ")
    chrome.wait("document.querySelectorAll('#sheet .hit').length === 2")
    assert api.searches == ["<img"]
    assert chrome.js("[...document.querySelectorAll('#sheet .hit mark')].map(m => m.textContent)") == ["<img", "kiraz"]
    assert chrome.js("document.querySelector('#sheet .hit .snip').textContent") == "x <img src=x onerror=window.pwned=1> y"
    assert chrome.js("[window.pwned === undefined, document.querySelectorAll('#sheet img').length]") == [True, 0]
    assert chrome.js("[...document.querySelectorAll('#sheet .hit small')].map(e => e.textContent.split(' · ')[0])") == ["Agent", "You"]
    assert chrome.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_typing_on_asks_once_for_the_last_word(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    look_for(chrome, "ki")
    look_for(chrome, "kir")
    look_for(chrome, "kiraz")
    chrome.wait("document.querySelector('#sheet .empty')?.textContent === 'Nothing found.'")
    assert api.searches == ["kiraz"]


def test_near_hit_loads_the_chat_from_there(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.messages, api.start = said("new", 30), 5000
    api.search = {"hits": [hit("new 0", [0, 3])], "partial": False}
    open_first(chrome, url)
    look_for(chrome, "new")
    chrome.click("#sheet .hit")
    chrome.wait("document.querySelector('#messages .msg.found') !== null")
    assert api.anchors[-1] == "700"
    assert chrome.js("document.querySelector('#messages .msg.found').textContent") == "new 0"
    assert chrome.js("document.querySelectorAll('#messages .msg.found').length") == 1
    chrome.wait("document.querySelector('#sheet').hidden")
    top = chrome.js("document.querySelector('#messages .msg.found').getBoundingClientRect().top - document.querySelector('#messages').getBoundingClientRect().top")
    assert 0 <= top < 200
    chrome.wait(f"document.querySelector('{EARLIER}')?.textContent === 'Load earlier'")
    chrome.wait("document.querySelector('#messages .msg.found') === null")


def test_far_or_past_hit_opens_the_read_only_sheet(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.search = {"hits": [hit("long ago", [0, 4], near=False), hit("then 1", [0, 4], conversation="past", title="Fix the build", near=False, end=90)], "partial": False}
    api.pages = {"conversation=&before=780": {"messages": said("far", 3), "start": 0}, "conversation=past&before=90": {"messages": said("then", 2), "start": 40},
                 "conversation=past&before=40": {"messages": said("first", 1), "start": 0}}
    open_first(chrome, url)
    look_for(chrome, "then")
    chrome.wait("document.querySelectorAll('#sheet .hit').length === 2")
    assert chrome.js("document.querySelectorAll('#sheet .hit')[1].textContent.includes('Fix the build')")
    chrome.click("#sheet .hit:nth-of-type(2)")
    chrome.wait("document.querySelectorAll('#sheet .past-chat .msg').length === 2")
    assert api.history == ["conversation=past&before=90"] and len(api.anchors) and "90" not in api.anchors
    assert chrome.js("[...document.querySelectorAll('#sheet .past-chat .msg.found')].map(e => e.textContent)") == ["then 1"]
    assert chrome.js("document.querySelector('#sheet h3').textContent") == "Fix the build"
    chrome.click("#sheet .past-more")
    chrome.wait("document.querySelectorAll('#sheet .past-chat .msg').length === 3")
    assert chrome.js("[...document.querySelectorAll('#sheet .past-chat .msg.found')].map(e => e.textContent)") == ["then 1"]
    chrome.js("document.querySelector('#sheet [data-close]').click()")
    chrome.wait("document.querySelector('#sheet').hidden")
    look_for(chrome, "long")
    chrome.click("#sheet .hit")
    chrome.wait("document.querySelectorAll('#sheet .past-chat .msg').length === 3")
    assert api.history[-1] == "conversation=&before=780"


def test_search_says_when_older_was_not_read_and_when_nothing_matched(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    api.search = {"hits": [hit("kiraz", [0, 5])], "partial": True}
    open_first(chrome, url)
    look_for(chrome, "kiraz")
    chrome.wait("document.querySelector('#sheet .partial')?.textContent === 'Older messages were not searched: it took too long.'")
    api.search = {"hits": [], "partial": True}
    look_for(chrome, "kira")
    chrome.wait("document.querySelector('#sheet .hit') === null && document.querySelector('#sheet .partial') !== null")
    assert chrome.js("document.querySelector('#sheet .empty') === null")
    api.search = {"hits": [], "partial": False}
    look_for(chrome, "elma")
    chrome.wait("document.querySelector('#sheet .empty')?.textContent === 'Nothing found.'")
    assert chrome.js("document.querySelector('#sheet .partial') === null")
    api.search_status = 404
    look_for(chrome, "armut")
    chrome.wait("document.querySelector('#sheet .empty')?.textContent === 'This session has closed.'")
    api.search_status = 409  # the session is there, it has no record to search: not "closed"
    look_for(chrome, "ayva")
    chrome.wait("document.querySelector('#sheet .empty')?.textContent === 'This session has no conversation to search.'")


def card_labels(chrome):
    return chrome.js("[...document.querySelectorAll('.card .c-act.choices [data-n]')].map(b => b.textContent)")


def test_card_choice_is_sent_once_from_the_card(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt(CHOICE)
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('.card .c-act.choices [data-n]').length === 3")
    assert card_labels(chrome) == ["SQLite", "Postgres", "MySQL"]
    assert chrome.js("document.querySelector('.card .c-cmd').textContent") == CHOICE["question"]
    api.respond.clear()
    chrome.click(".card [data-n='2']")
    chrome.wait("document.querySelector('.card [data-n=\"3\"]').getAttribute('aria-disabled') === 'true'")
    chrome.click(".card [data-n='3']")  # ignored while the first answer is on its way
    api.respond.set()
    chrome.wait("document.querySelector('#toasts .ok')?.textContent === 'Reply sent'")
    api.publish_session("working")
    chrome.wait("document.querySelector('.card [data-n]') === null")
    assert api.replies == [("choose", 2, CHOICE["fingerprint"])]
    assert len(api.requests) == 1
    assert chrome.js("document.querySelector('#session').hidden")  # answering does not open the session


def test_card_choice_refused_for_a_changed_screen_shows_the_new_question(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt(CHOICE)
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('.card .c-act.choices [data-n]').length === 3")
    changed = {**CHOICE, "fingerprint": "second-choice", "question": "Which cache?",
               "options": [{"n": 1, "label": "Redis"}, {"n": 2, "label": "None"}]}
    api.approval, api.cards = changed, [card("blocked", pending=changed)]  # the server sees the change before the browser does
    chrome.click(".card [data-n='1']")
    chrome.wait("document.querySelector('#toasts .err')?.textContent === 'The screen changed; refresh'")
    chrome.wait("document.querySelector('.card .c-cmd')?.textContent === 'Which cache?'")
    assert card_labels(chrome) == ["Redis", "None"]
    assert not api.replies


def test_card_choice_that_does_not_fit_has_no_buttons_on_the_card(api_fixture):
    chrome, api, url = api_fixture
    api.publish_prompt({**CHOICE, "options": []})
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card .c-cmd')?.textContent === 'Which database?'")
    assert chrome.js("document.querySelector('.card .c-act') === null")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_card_choice_with_four_long_labels_fits_320_px(api_fixture, theme):
    chrome, api, url = api_fixture
    labels = ["W" * 40, "Supercalifragilisticexpialidocious-ğüşiöç", "İstanbul'da bir seçenek daha 🙂🙂🙂🙂🙂🙂"[:40], "m i " * 10]
    api.publish_prompt({**CHOICE, "question": "Q" * 160, "roles": ["choice"] * 4,
                        "options": [{"n": n, "label": label[:40]} for n, label in enumerate(labels, 1)]})
    chrome.resize(320, 640)
    chrome.cdp("Page.navigate", url=url)
    chrome.js(f"localStorage.theme = {json.dumps(theme)}")
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelectorAll('.card .c-act.choices [data-n]').length === 4")
    assert chrome.js("""(() => {
        const card = document.querySelector('.card').getBoundingClientRect();
        const buttons = [...document.querySelectorAll('.card [data-n]')];
        const command = document.querySelector('.card .c-cmd');
        return document.documentElement.scrollWidth <= 320 && command.scrollHeight <= command.clientHeight
            && buttons.every(b => { const r = b.getBoundingClientRect(); return r.left >= card.left && r.right <= card.right && r.height >= 44 && b.scrollWidth <= b.clientWidth && b.scrollHeight <= b.clientHeight; });
    })()""")


CHIPS = "[...document.querySelectorAll('#chips button')].map(b => b.textContent)"
LISTED = "[...document.querySelectorAll('#sheet .chip-list li span')].map(e => e.textContent)"
CHIP_ROW = "(() => { const c = document.querySelector('#chips'); return !!c && c.getClientRects().length > 0; })()"


def with_chips(chrome, url, chips):
    chrome.cdp("Page.navigate", url=url)
    chrome.js(f"localStorage.setItem('chips', {json.dumps(json.dumps(chips))})")
    open_first(chrome, url)


def open_chips(chrome):
    open_settings(chrome)
    chrome.click("#sheet #chipsRow")
    chrome.wait("document.querySelector('#sheet .chip-new input') !== null")


def add_chip(chrome, text):
    chrome.js(f"(() => {{ const f = document.querySelector('#sheet .chip-new'); f.querySelector('input').value = {json.dumps(text)}; f.requestSubmit(); }})()")


def test_no_chip_row_until_one_is_defined(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    assert chrome.js(CHIP_ROW) is False and chrome.js(CHIPS) == []
    open_chips(chrome)
    add_chip(chrome, "go on")
    chrome.wait(CHIP_ROW)
    assert chrome.js(CHIPS) == ["go on"]


def test_a_chip_goes_into_the_draft_at_the_cursor_and_is_not_sent(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    with_chips(chrome, url, ["X"])
    chrome.wait(CHIP_ROW)
    chrome.js("(() => { const t = document.querySelector('#message'); t.value = 'ab'; t.dispatchEvent(new Event('input')); t.setSelectionRange(1, 1); t.blur(); })()")
    down = chrome.js("(() => { const e = new PointerEvent('pointerdown', {bubbles: true, cancelable: true}); document.querySelector('#chips button').dispatchEvent(e); return e.defaultPrevented; })()")
    chrome.click("#chips button")
    assert down is True  # the keyboard stays up
    assert chrome.js("document.querySelector('#message').value") == "aXb"
    assert chrome.js("document.activeElement.id") == "message" and chrome.js("document.querySelector('#message').selectionStart") == 2
    assert chrome.js("localStorage.getItem('draft:fake:p1')") == "aXb" and chrome.js("document.querySelector('#send').disabled") is False
    time.sleep(0.3)
    assert api.prompts == []


def test_chips_are_added_removed_and_reordered_in_settings(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    open_chips(chrome)
    assert chrome.js("document.querySelector('#sheet .chip-list li')") is None and chrome.js("!!document.querySelector('#sheet .chip-empty')")
    for text in ["  one  ", "two", "three", "   ", "L" * 201]:
        add_chip(chrome, text)
    assert chrome.js(LISTED) == ["one", "two", "three", "L" * 200]
    assert chrome.js("document.querySelector('#sheet .chip-new input').value") == ""
    chrome.click("#sheet .chip-list li:nth-child(3) [data-chip=up]")
    assert chrome.js(LISTED) == ["one", "three", "two", "L" * 200]
    chrome.click("#sheet .chip-list li:nth-child(1) [data-chip=down]")
    assert chrome.js(LISTED) == ["three", "one", "two", "L" * 200]
    assert chrome.js("document.querySelector('#sheet .chip-list li:first-child [data-chip=up]').disabled && document.querySelector('#sheet .chip-list li:last-child [data-chip=down]').disabled")
    chrome.click("#sheet .chip-list li:nth-child(4) [data-chip=drop]")
    assert chrome.js(LISTED) == ["three", "one", "two"] and chrome.js(CHIPS) == ["three", "one", "two"]
    for n in range(9):
        add_chip(chrome, f"more {n}")
    assert chrome.js("document.querySelectorAll('#sheet .chip-list li').length") == 12
    assert chrome.js("document.querySelector('#sheet .chip-new button').disabled && document.querySelector('#sheet .chip-new input').disabled")
    add_chip(chrome, "a thirteenth")
    assert chrome.js("document.querySelectorAll('#sheet .chip-list li').length") == 12
    assert chrome.js("[...document.querySelectorAll('#sheet .chip-list button')].filter(b => !(b.getBoundingClientRect().height >= 44 && b.getAttribute('aria-label'))).map(b => [b.dataset.chip, b.dataset.i, b.getBoundingClientRect().height])") == []
    assert chrome.js("document.querySelector('#sheet .chip-off').hidden") is True
    open_first(chrome, url)
    chrome.wait(CHIP_ROW)
    assert chrome.js(CHIPS)[:3] == ["three", "one", "two"] and len(chrome.js(CHIPS)) == 12


def test_chips_live_in_memory_and_say_so_when_the_browser_keeps_nothing(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    chrome.js("Storage.prototype.setItem = () => { throw new Error('full'); }")
    open_chips(chrome)
    add_chip(chrome, "kept for now")
    assert chrome.js(CHIPS) == ["kept for now"]
    assert chrome.js("document.querySelectorAll('#sheet .chip-off:not([hidden])').length") == 1 and chrome.js("document.querySelector('#toasts .err')") is None


def test_stored_chips_that_are_not_short_texts_are_left_out(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    with_chips(chrome, url, ["ok", 7, "", "  ", None, {"a": 1}, "W" * 300] + [f"c{n}" for n in range(20)])
    chrome.wait(CHIP_ROW)
    chips = chrome.js(CHIPS)
    assert chips[:3] == ["ok", "W" * 200, "c0"] and len(chips) == 12
    chrome.js("localStorage.setItem('chips', '{not json')")
    open_first(chrome, url)
    assert chrome.js(CHIP_ROW) is False and chrome.js("document.querySelector('#toasts .err')") is None


def test_chip_row_hides_under_a_prompt_and_the_slash_menu(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    with_chips(chrome, url, ["yes, go on"])
    chrome.wait(CHIP_ROW)
    chrome.js("(() => { const m = document.querySelector('#message'); m.value = '/'; m.dispatchEvent(new Event('input', {bubbles: true})); })()")
    chrome.wait("!document.querySelector('#slashOptions').hidden")
    assert chrome.js(CHIP_ROW) is False
    chrome.js("(() => { const m = document.querySelector('#message'); m.value = ''; m.dispatchEvent(new Event('input', {bubbles: true})); })()")
    chrome.wait(CHIP_ROW)
    api.publish_prompt(APPROVAL)
    chrome.wait(f"document.querySelector({json.dumps(APPROVE)}) !== null")
    assert chrome.js(CHIP_ROW) is False
    api.publish_session("idle")
    chrome.wait(CHIP_ROW)


def test_a_chip_is_text_not_markup(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    bad = "<img src=x onerror=window.pwned=1>"
    with_chips(chrome, url, [bad])
    chrome.wait(CHIP_ROW)
    open_chips(chrome)
    add_chip(chrome, '"><img src=x onerror=window.pwned=2>')
    assert chrome.js(LISTED) == [bad, '"><img src=x onerror=window.pwned=2>'] and chrome.js(CHIPS)[0] == bad
    chrome.click("#chips button")
    time.sleep(0.2)
    assert chrome.js("document.querySelector('#message').value") == bad
    assert chrome.js("window.pwned") is None and chrome.js("document.querySelector('#chips img, #sheet img')") is None


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_a_long_chip_row_scrolls_without_widening_the_page(api_fixture, theme):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    chrome.resize(320, 640)
    chrome.cdp("Page.navigate", url=url)
    chrome.js(f"localStorage.theme = {json.dumps(theme)}")
    with_chips(chrome, url, [chr(65 + n) * 200 for n in range(12)])
    chrome.wait("document.querySelector('#chips').getBoundingClientRect().left === 8")  # the session has slid in
    assert chrome.js("document.documentElement.scrollWidth === document.documentElement.clientWidth") and chrome.js("document.documentElement.clientWidth") == 320
    assert chrome.js("""(() => {
        const row = document.querySelector('#chips'), buttons = [...row.querySelectorAll('button')], box = row.getBoundingClientRect();
        const send = document.querySelector('#send').getBoundingClientRect(), ta = document.querySelector('#message').getBoundingClientRect();
        return row.scrollWidth > row.clientWidth && box.left >= 0 && box.right <= 320 && box.bottom <= ta.top && send.right <= 320 && Math.abs(send.bottom - ta.bottom) < 2
            && buttons.every(b => { const r = b.getBoundingClientRect(); return Math.round(r.height) === 44 && r.width <= 300 && b.getAttribute('aria-label').length === 200; });
    })()""")
    open_chips(chrome)
    assert chrome.js("""(() => {
        const sheet = document.querySelector('#sheet').getBoundingClientRect();
        return document.documentElement.scrollWidth === 320 && [...document.querySelectorAll('#sheet .chip-list li, #sheet .chip-list button, #sheet .chip-new *')].every(e => { const r = e.getBoundingClientRect(); return r.left >= sheet.left && r.right <= sheet.right; });
    })()""")


# --- the whole built-in list, and a question before a command that ends the session ---

BUILTINS = [
    {"name": "/clear", "description": "", "source": "builtin", "kind": "text"},
    {"name": "/model", "description": "", "source": "builtin", "kind": "menu"},
    {"name": "/exit", "description": "", "source": "builtin", "kind": "ends"},
    {"name": "/export", "description": "", "source": "builtin", "kind": "menu"},
    {"name": "/deploy", "description": "Ship the panel", "source": "user", "kind": "text"},
]


def type_message(chrome, text):
    chrome.js(f"(() => {{ const m = document.querySelector('#message'); m.focus(); m.value = {json.dumps(text)}; m.dispatchEvent(new Event('input', {{bubbles: true}})); }})()")


def slash_rows(chrome):
    return chrome.js("[...document.querySelectorAll('#slashOptions [data-slash-cmd]')].map(b => [b.dataset.slashCmd, b.querySelector('.note')?.textContent || ''])")


def test_slash_menu_lists_exit_and_says_which_builtin_opens_a_menu(api_fixture):
    chrome, api, url = api_fixture
    api.commands = BUILTINS
    chrome.resize(320, 568)
    open_first(chrome, url)
    chrome.wait("slashCommands().some(c => c.name === '/exit')")
    type_message(chrome, "/ex")
    chrome.wait("!document.querySelector('#slashOptions').hidden")
    assert slash_rows(chrome) == [["/exit", ""], ["/export", "opens a menu in the terminal"]]
    type_message(chrome, "/")
    assert dict(slash_rows(chrome)) == {"/clear": "", "/model": "opens a menu in the terminal", "/exit": "", "/export": "opens a menu in the terminal", "/deploy": ""}
    assert chrome.js("document.querySelector('#slashOptions [data-slash-cmd=\"/deploy\"] small').textContent") == "Ship the panel"
    assert chrome.js("[...document.querySelectorAll('#slashOptions .note')].every(n => { const r = n.getBoundingClientRect(); return r.width > 0 && r.right <= innerWidth; })")


def submit_message(chrome, text):
    type_message(chrome, text)
    chrome.js("document.querySelector('#composer').requestSubmit()")


def test_exit_asks_once_before_it_is_sent_and_cancel_keeps_the_draft(api_fixture):
    chrome, api, url = api_fixture
    api.commands = BUILTINS
    api.publish_session("idle")
    open_first(chrome, url)
    chrome.wait("slashCommands().some(c => c.kind === 'ends')")
    submit_message(chrome, "/exit")
    chrome.wait("!document.querySelector('#sheet').hidden && document.querySelector('#sheet [data-ends]') !== null")
    assert chrome.js("document.querySelector('#sheet h3').textContent") == "Close this session?"
    assert "/exit" in chrome.js("document.querySelector('#sheet p').textContent")
    assert chrome.js("document.querySelector('#sheet [data-ends]').textContent") == "Send /exit"
    assert api.prompts == []
    chrome.click("#sheet [data-x]")
    chrome.wait("document.querySelector('#sheet').hidden")
    time.sleep(0.2)
    assert api.prompts == [] and chrome.js("document.querySelector('#message').value") == "/exit"
    chrome.js("document.querySelector('#composer').requestSubmit()")
    chrome.wait("document.querySelector('#sheet [data-ends]') !== null")
    chrome.js("(() => { const b = document.querySelector('#sheet [data-ends]'); b.click(); b.click(); })()")
    wait_for(lambda: api.prompts == ["/exit"], "the command to be sent")
    chrome.wait("document.querySelector('#sheet').hidden")
    time.sleep(0.3)
    assert api.prompts == ["/exit"] and chrome.js("document.querySelector('#message').value") == ""


def test_exit_inside_a_sentence_and_plain_commands_are_sent_without_asking(api_fixture):
    chrome, api, url = api_fixture
    api.commands = BUILTINS
    api.publish_session("idle")
    open_first(chrome, url)
    chrome.wait("slashCommands().some(c => c.kind === 'ends')")
    for text in ["what does /exit do", "/clear", "/exits", "/model"]:
        submit_message(chrome, text)
        wait_for(lambda: api.prompts[-1:] == [text], f"{text!r} to be sent")
        assert chrome.js("document.querySelector('#sheet [data-ends]') === null")


def test_exit_asks_even_when_the_command_list_did_not_load(api_fixture):
    chrome, api, url = api_fixture
    api.publish_session("idle")
    open_first(chrome, url)
    for text in ["/exit", "/quit now"]:
        submit_message(chrome, text)
        chrome.wait("!document.querySelector('#sheet').hidden && document.querySelector('#sheet [data-ends]') !== null")
        chrome.click("#sheet [data-x]")
        chrome.wait("document.querySelector('#sheet').hidden")
    assert api.prompts == []


# --- an over-long command: all of it when the record pins it, the reason when it does not ---

LONG = "\n".join(f"echo line-{i:04d} " + "x" * 60 for i in range(280))  # about 20 KB
FULL = {**APPROVAL, "fingerprint": "full-approval", "details": LONG[:300], "cut": True, "approvable": True, "full": LONG, "full_source": "transcript",
        "roles": ["approve", "always", "deny"], "options": [{"role": "approve"}, {"role": "always"}, {"role": "deny"}]}


def test_full_command_ui_shows_the_whole_command_in_a_box_that_scrolls(api_fixture):
    chrome, api, url = api_fixture
    assert 19000 < len(LONG) < 22000
    chrome.resize(320, 568)
    api.publish_prompt({**FULL, "cut": False, "approvable": None, "full": None, "fingerprint": "short"})
    open_first(chrome, url)
    chrome.wait("document.querySelector('#promptZone [data-d=always]') !== null")  # the same answers on a command that fits: session-wide allow is offered
    api.publish_prompt(FULL)
    chrome.wait("document.querySelector('#promptZone pre.full') !== null")
    assert chrome.js("document.querySelector('#promptZone pre.full').textContent") == LONG
    assert chrome.js("document.querySelectorAll('#promptZone pre').length") == 1
    assert chrome.js("document.querySelector('#promptZone .full-note').textContent") == "The whole command, read from the session record."
    box = chrome.js("(() => { const e = document.querySelector('#promptZone pre.full'), r = e.getBoundingClientRect(); return {h: r.height, max: innerHeight * .45, scrolls: e.scrollHeight > e.clientHeight + 100, flow: getComputedStyle(e).overflowY, top: r.top}; })()")
    assert box["h"] <= box["max"] and box["scrolls"] and box["flow"] == "auto" and box["top"] >= 0
    assert chrome.js("document.querySelector('#promptZone [data-d=approve]') !== null && document.querySelector('#promptZone [data-d=deny]') !== null")
    assert chrome.js("document.querySelector('#promptZone [data-d=always]') === null")
    chrome.wait("document.querySelector('#session').getBoundingClientRect().left === 0")  # the screen has finished sliding in
    seen = chrome.js("""(() => ['#message', '#send', '#promptZone [data-d=approve]', '#promptZone [data-d=deny]', '#promptZone .full-note'].map(q => {
        const r = document.querySelector(q).getBoundingClientRect(); return r.height > 0 && r.top >= 0 && r.bottom <= innerHeight && r.right <= innerWidth; }))()""")
    assert seen == [True] * 5
    assert chrome.js("document.querySelector('#promptZone [data-d=approve]').getBoundingClientRect().bottom <= document.querySelector('#composer').getBoundingClientRect().top")
    assert chrome.js("document.documentElement.scrollWidth") == 320
    chrome.click(APPROVE)
    wait_for(lambda: api.requests, "the approval")
    assert api.requests[-1]["fingerprint"] == "full-approval" and api.requests[-1]["decision"] == "approve"


@pytest.mark.parametrize("reason, words", [
    ("no_record", "this session has no record to read it from"),
    ("no_match", "two commands at once"),
    ("too_long", "longer than 64 KB"),
    (None, "does not fit on screen, so it can only be denied"),
])
def test_full_command_ui_only_denies_and_says_why_when_the_record_does_not_pin_it(api_fixture, reason, words):
    chrome, api, url = api_fixture
    chrome.resize(320, 568)
    api.publish_prompt({**FULL, "approvable": False, "full": None, "cut_reason": reason, "fingerprint": f"cut-{reason}"})
    open_first(chrome, url)
    chrome.wait("document.querySelector('#promptZone [data-d=deny]') !== null")
    assert chrome.js("document.querySelector('#promptZone [data-d=approve]') === null && document.querySelector('#promptZone [data-d=always]') === null")
    assert chrome.js("document.querySelector('#promptZone pre.full') === null && document.querySelector('#promptZone .full-note') === null")
    note = chrome.js("document.querySelector('#promptZone .cut-note').textContent")
    assert words in note and "denied" in note
    floor = chrome.js("document.querySelector('#composer').getBoundingClientRect().top")
    for q in ["#promptZone .cut-note", "#promptZone [data-d=deny]", "#promptZone pre"]:  # the reason and the one answer are in view, the command gives way
        r = chrome.js(f"(() => {{ const r = document.querySelector({json.dumps(q)}).getBoundingClientRect(); return [r.width > 0, r.right <= innerWidth, r.top >= 0, r.bottom]; }})()")
        assert r[:3] == [True, True, True] and r[3] <= floor, (q, r, floor)


# --- the home card: context and cache under the state line ---
CARD_METERS = "document.querySelector('.card .c-meters')?.textContent"


def home(chrome, api, url, lang="", **fields):
    api.cards = [card("idle", **fields)]
    chrome.cdp("Page.navigate", url=url)
    if lang:
        chrome.js(f"localStorage.setItem('lang', {json.dumps(lang)})")
        chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card .state') !== null")


def test_card_meters_say_context_and_cache_under_the_state_line(api_fixture):
    chrome, api, url = api_fixture
    home(chrome, api, url, meters=meters())
    assert chrome.js(CARD_METERS) == "Context 124k · Cache ~41m"
    assert chrome.js("document.querySelector('.card .state').nextElementSibling.className") == "c-meters"
    chrome.click(".card .c-meters")  # the line is part of the card, not a dead spot on it
    chrome.wait("!document.querySelector('#session').hidden")
    home(chrome, api, url, lang="tr", meters=meters())
    assert chrome.js(CARD_METERS) == "Bağlam 124k · Cache ~41 dk"


def test_card_meters_call_an_expired_cache_cold_without_alarm(api_fixture):
    chrome, api, url = api_fixture
    home(chrome, api, url, meters=meters(age=7200))
    assert chrome.js(CARD_METERS) == "Context 124k · Cache cold"
    colours = chrome.js("""(() => { const of = v => { const p = document.createElement('i'); p.style.color = `var(${v})`; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; };
        return [getComputedStyle(document.querySelector('.card .c-meters')).color, of('--muted'), of('--lal'), of('--lal-t')]; })()""")
    assert colours[0] == colours[1] and colours[0] not in colours[2:]


def test_card_meters_leave_out_what_is_not_known(api_fixture):
    chrome, api, url = api_fixture
    home(chrome, api, url, meters={"context": meters()["context"]})
    assert chrome.js(CARD_METERS) == "Context 124k"
    for nothing in ({}, None, {"agents": {"running": 0}}, {"context": {"tokens": "<b>9</b>"}, "cache": {"ttl": "1", "since": None}}):  # e.g. right after a compaction
        home(chrome, api, url, meters=nothing)
        assert chrome.js("document.querySelector('.card .c-meters')") is None
    api.cards = [card("idle")]  # an older server sends no key at all
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card .state') !== null")
    assert chrome.js("document.querySelector('.card .c-meters')") is None


def test_card_meters_count_the_agents_still_out_and_stay_on_one_line_at_320_px(api_fixture):
    chrome, api, url = api_fixture
    chrome.resize(320, 640)
    home(chrome, api, url, meters={**meters(), "agents": {"running": 12}}, title="a title")
    assert chrome.js(CARD_METERS) == "12 subagents · Context 124k · Cache ~41m"
    home(chrome, api, url, lang="tr", meters={**meters(tokens=1_240_000), "agents": {"running": 12}})
    assert chrome.js(CARD_METERS) == "12 alt ajan · Bağlam 1.2M · Cache ~41 dk"
    box = chrome.js("(() => { const m = document.querySelector('.card .c-meters'), r = m.getBoundingClientRect(), c = document.querySelector('.card').getBoundingClientRect(); return [r.height, parseFloat(getComputedStyle(m).fontSize), r.left >= c.left, r.right <= c.right, m.scrollWidth <= m.clientWidth, document.documentElement.scrollWidth]; })()")
    assert box[0] <= 20 and 12 <= box[1] <= 13 and box[2:] == [True, True, True, 320], box  # the cache is the last word and is not cut
    home(chrome, api, url, meters={"agents": {"running": 1}})
    assert chrome.js(CARD_METERS) == "1 alt ajan"


def test_card_meters_minute_moves_without_asking_for_the_list_again(api_fixture):
    chrome, api, url = api_fixture
    home(chrome, api, url, meters=meters())
    assert chrome.js(CARD_METERS) == "Context 124k · Cache ~41m"
    api.list_started.clear()
    chrome.js("state.skew = (state.skew || 0) + 600")  # ten minutes pass on the computer's clock
    chrome.wait(f"{CARD_METERS} === 'Context 124k · Cache ~31m'")
    chrome.js("state.skew += 3600")
    chrome.wait(f"{CARD_METERS} === 'Context 124k · Cache cold'")
    assert not api.list_started.is_set()


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_the_one_answer_to_a_cut_command_stays_in_view_with_the_keyboard_up(api_fixture, lang):
    # a phone with a notch and the keyboard open leaves about 480 px; Deny is the only answer and must not need a scroll
    chrome, api, url = api_fixture
    chrome.resize(390, 480)
    chrome.cdp("Emulation.setSafeAreaInsetsOverride", insets={"top": 47, "bottom": 0})
    chrome.cdp("Page.navigate", url=url)
    chrome.js(f"localStorage.setItem('lang', {json.dumps(lang)})")
    api.publish_prompt({**FULL, "approvable": False, "full": None, "cut_reason": "no_match", "fingerprint": "cut-short"})
    open_first(chrome, url)
    chrome.wait("document.querySelector('#promptZone [data-d=deny]') !== null")
    fits = "(() => { const z = document.querySelector('#promptZone'), d = z.querySelector('[data-d=deny]').getBoundingClientRect(), p = z.querySelector('pre').getBoundingClientRect(); return [d.bottom <= document.querySelector('#composer').getBoundingClientRect().top, z.scrollHeight <= z.clientHeight + 1, p.height >= 36, document.querySelector('#messages').getBoundingClientRect().height > 0]; })()"
    wait_for(lambda: chrome.js(fits) == [True, True, True, True], f"Deny in view: {chrome.js(fits)}", timeout=3)


def test_the_measuring_strip_sends_what_it_measured_to_the_machine_and_not_the_conversation(api_fixture):
    chrome, api, url = api_fixture
    api.messages = [{"role": "assistant", "text": "Nobody else reads this answer"}]
    open_first(chrome, url + "?debug=1")
    chrome.js("document.body.insertAdjacentHTML('beforeend', '<div id=wide class=\"probe one\" style=\"position:fixed;left:0;top:0;width:2000px;height:1px\">secret words</div>')")
    chrome.js("window.dispatchEvent(new ErrorEvent('error', {message: 'boom at line 7'}))")
    chrome.click("#debugSend")
    wait_for(lambda: api.files, "the report to arrive")
    assert api.uploads[0][0] == "text/plain"
    report = api.files[0].decode()
    for said in ("vv.h=", "standalone=false", "ua=", "view=", "dpr=", "scheme=", "div#wide.probe.one", "boom at line 7", "#composer "):
        assert said in report, said
    assert "Nobody else reads" not in report and "secret words" not in report and "Example" not in report
    chrome.wait("document.querySelector('#debugSend').textContent === 'sent: up-1'")
    api.files.clear()
    chrome.cdp("Page.navigate", url=url)  # without the strip there is no button and nothing is sent
    chrome.wait("document.querySelector('.card-open') !== null")
    assert chrome.js("document.querySelector('#debugSend') === null") and not api.files



# --- automatic handoff: the switch, the countdown and the way out ---
OFF = {"on": False, "phase": "armed", "due": None}
HAND = "document.querySelector('.card .c-meters [data-handoff]')?.textContent"
SWITCH = "document.querySelector('#sheet [role=switch]')"
LONG_NAME = "a-project-name-of-forty-characters-long-x"[:40]


def written(minutes=7):
    return {"on": True, "phase": "written", "due": time.time() + minutes * 60 - 20}


def token(chrome, name):
    return chrome.js(f"(() => {{ const p = document.createElement('i'); p.style.color = 'var({name})'; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; }})()")


def open_meters(chrome, api, url, lang="en", measured=None, **fields):
    api.cards, api.meters = [card("idle", **fields)], measured or {}
    chrome.cdp("Page.navigate", url=url)
    chrome.js(f"localStorage.setItem('lang', {json.dumps(lang)})")
    open_first(chrome, url)
    chrome.wait(SHOWN)
    chrome.click("#sMeta")
    chrome.wait("document.querySelector('#sheet h3') !== null")


def test_meters_sheet_turns_automatic_handoff_on(api_fixture):
    chrome, api, url = api_fixture
    api.config = {**api.config, "auto_handoff": {"min_tokens": 80000, "grace_seconds": 600}}
    open_meters(chrome, api, url, handoff=OFF)  # nothing measured yet: the line names the switch, and the sheet opens on it alone
    assert chrome.js(META) == "Automatic handoff off"
    assert chrome.js(f"[{SWITCH}.getAttribute('aria-checked'), {SWITCH}.textContent, {SWITCH}.getBoundingClientRect().height >= 44, {SWITCH}.getBoundingClientRect().right <= innerWidth]") == ["false", "Automatic handoff", True, True]
    assert chrome.js("document.querySelector('#sheet .switch-note').textContent") == "Has the handoff written at minute 50 and clears 10 min later"
    api.respond.clear()
    chrome.click("#sheet [role=switch]")
    wait_for(lambda: api.handoff_calls, "the switch to reach the server")
    assert chrome.js(f"{SWITCH}.disabled") is True
    chrome.js(f"{SWITCH}.click()")  # a second tap while the first is on its way sends nothing
    api.respond.set()
    chrome.wait(f"{SWITCH}.getAttribute('aria-checked') === 'true' && !{SWITCH}.disabled")
    assert api.handoff_calls == [("PUT", {"on": True})]
    chrome.wait(f"{META} === 'Automatic handoff on'")
    api.handoff_fail = True  # the server could not keep it: the switch goes back and says why
    chrome.click("#sheet [role=switch]")
    chrome.wait("document.querySelector('.toast.err') !== null")
    assert chrome.js(f"[{SWITCH}.getAttribute('aria-checked'), {SWITCH}.disabled]") == ["true", False]
    assert api.handoff_calls[1] == ("PUT", {"on": False})
    api.config = {**api.config, "auto_handoff": {"min_tokens": 80000, "grace_seconds": 300}}
    open_meters(chrome, api, url, lang="tr", measured=meters(), handoff=OFF)
    assert chrome.js("document.querySelectorAll('#sheet .meter').length") == 2  # the switch is the last row, under what was measured
    assert chrome.js(f"[{SWITCH}.textContent, document.querySelector('#sheet .switch-note').textContent, {SWITCH}.compareDocumentPosition(document.querySelector('#sheet .meter')) & 2]") == ["Otomatik devir", "50. dakikada devir yazdırır, 5 dk sonra temizler", 2]


def test_codex_session_has_no_handoff_switch(api_fixture):
    chrome, api, url = api_fixture
    open_meters(chrome, api, url, agent="codex", measured=meters())  # the server sends the field for Claude sessions only
    assert chrome.js("document.querySelectorAll('#sheet .meter').length") == 2
    assert chrome.js(f"{SWITCH} === null && document.querySelector('#sheet .switch-note') === null")
    api.cards, api.meters = [card("idle", agent="codex")], {}
    open_first(chrome, url)
    assert chrome.js("document.querySelector('#sMeta').hidden && document.querySelector('#handBar').hidden")


@pytest.mark.parametrize("width", [320, 390])
def test_written_handoff_leads_the_meter_line_and_can_be_cancelled(api_fixture, width):
    chrome, api, url = api_fixture
    chrome.resize(width, 700)
    home(chrome, api, url, project=LONG_NAME, handoff=written(), meters={**meters(), "agents": {"running": 3}})
    # a session is asked after fifty idle minutes, and the idle group is folded: the card comes up to where it is seen
    assert chrome.js("[document.querySelector('#idleCards'), document.querySelector('.g-done .card')?.dataset.key, document.querySelector('.card').className]") == [None, PANE, "card b-idle"]
    assert chrome.js(HAND) == "handoff written · clears in 7 min"
    assert chrome.js("document.querySelector('.card .c-meters').textContent").startswith("handoff written · clears in 7 min · 3 subagents")
    cancel = "document.querySelector('.card [data-handoff-cancel]')"
    box = chrome.js(f"(() => {{ const b = {cancel}, r = b.getBoundingClientRect(), h = document.querySelector('.card [data-handoff]').getBoundingClientRect(), c = document.querySelector('.card').getBoundingClientRect(), hit = getComputedStyle(b, '::before'); return [b.textContent, r.width > 0, r.right <= innerWidth && r.right <= c.right, h.right <= r.left, r.height + 2 * Math.abs(parseFloat(hit.top)), document.documentElement.scrollWidth, document.elementFromPoint(r.left + r.width / 2, r.top - 8) === b]; }})()")
    assert box[:4] == ["Cancel", True, True, True] and box[4] >= 44 and box[5:] == [width, True], box  # the countdown is whole, the finger has 44 px
    colours = chrome.js(f"[getComputedStyle({cancel}).color, getComputedStyle({cancel}).borderTopColor, getComputedStyle({cancel}).backgroundColor]")
    assert colours[0] == token(chrome, "--strong") and colours[1] == token(chrome, "--line") and not {token(chrome, "--lal"), token(chrome, "--lal-t")} & set(colours)
    api.respond.clear()
    chrome.js(f"{cancel}.click()")
    wait_for(lambda: api.handoff_calls, "Cancel to reach the server")
    chrome.js(f"{cancel}?.click()")
    api.respond.set()
    chrome.wait(f"{cancel} === null")  # the card answers with the session as it now stands
    assert api.handoff_calls == [("POST", {})]
    assert chrome.js("document.querySelector('#session').hidden") is True  # Cancel is not a tap on the card
    assert chrome.js(CARD_METERS) == "3 subagents · Context 124k · Cache ~41m"
    # the longest it gets: Turkish, two-digit minutes, a card with a border; the countdown is never the part that is cut
    api.cards = [card("blocked", pending=APPROVAL, project=LONG_NAME, handoff=written(10), meters={**meters(), "agents": {"running": 3}})]
    chrome.js("localStorage.setItem('lang', 'tr')")
    chrome.cdp("Page.navigate", url=url)
    chrome.wait(f"{HAND} === 'devir yazıldı · 10 dk sonra temizlenecek'")
    fit = chrome.js(f"(() => {{ const h = document.querySelector('.card [data-handoff]'), s = h.parentElement.getBoundingClientRect(), x = {cancel}.getBoundingClientRect(), r = document.createRange(); r.selectNodeContents(h); return [[...r.getClientRects()].every(b => b.left >= s.left - 1 && b.right <= s.right + 1 && b.bottom <= s.bottom + 1), s.right <= x.left, s.height <= 36, x.right <= document.querySelector('.card').getBoundingClientRect().right, document.documentElement.scrollWidth]; }})()")
    assert fit == [True, True, True, True, width], fit


@pytest.mark.parametrize("width", [320, 390])
def test_open_session_says_the_handoff_countdown_whole_with_cancel_beside_it(api_fixture, width):
    chrome, api, url = api_fixture
    chrome.resize(width, 700)
    api.meters, api.limits = meters(), [limit("claude", ("5h", 93, None))]  # the ring leaves the header line 94 px at 320
    api.cards = [card("idle", project=LONG_NAME, handoff=written())]
    chrome.cdp("Page.navigate", url=url)
    chrome.js("localStorage.setItem('lang', 'tr')")
    open_first(chrome, url)
    chrome.wait("!document.querySelector('#handBar').hidden && !document.querySelector('#sRing').hidden && document.querySelector('#session').getAnimations().length === 0")
    assert chrome.js(META).startswith("devir yazıldı · 7 dk sonra temizlenecek · Bağlam")
    bar = chrome.js("(() => { const b = document.querySelector('#handBar'), s = b.querySelector('span'), x = b.querySelector('[data-handoff-cancel]'), r = x.getBoundingClientRect(), hit = getComputedStyle(x, '::before'); return [s.textContent, x.textContent, s.scrollWidth <= s.clientWidth, r.right <= innerWidth, r.height + 2 * Math.abs(parseFloat(hit.top)) >= 44, b.getBoundingClientRect().bottom <= document.querySelector('#composer').getBoundingClientRect().top, document.documentElement.scrollWidth, b.getAttribute('role')]; })()")
    assert bar == ["devir yazıldı · 7 dk sonra temizlenecek", "Vazgeç", True, True, True, True, width, "status"], bar
    chrome.click("#handBar [data-handoff-cancel]")
    chrome.wait("document.querySelector('#handBar').hidden")
    assert api.handoff_calls == [("POST", {})]
    chrome.wait(f"{META}.startsWith('Bağlam')")


def test_asked_handoff_says_it_is_being_written(api_fixture):
    chrome, api, url = api_fixture
    asked = {"on": True, "phase": "asked", "due": None}
    home(chrome, api, url, handoff=asked)  # nothing else is known about the session: the line is there all the same
    assert chrome.js(CARD_METERS) == "writing handoffCancel"
    home(chrome, api, url, lang="tr", handoff=asked, meters=meters())
    assert chrome.js(HAND) == "devir yazılıyor" and chrome.js("document.querySelector('.card [data-handoff-cancel]').textContent") == "Vazgeç"
    assert chrome.js("document.querySelector('.card .c-meters').firstElementChild.firstElementChild.matches('[data-handoff]')")
    for quiet in (OFF, {"on": True, "phase": "armed", "due": None}, {"on": True, "phase": "held", "due": None}, {"on": True, "phase": "<b>new</b>", "due": None}):
        home(chrome, api, url, handoff=quiet, meters=meters())
        assert chrome.js(CARD_METERS) == "Bağlam 124k · Cache ~41 dk" and chrome.js("document.querySelector('.card [data-handoff-cancel]')") is None


def test_handoff_countdown_moves_with_the_minute_and_ends_in_shortly(api_fixture):
    chrome, api, url = api_fixture
    home(chrome, api, url, handoff=written(10))
    assert chrome.js(HAND) == "handoff written · clears in 10 min"
    api.list_started.clear()
    chrome.js("state.skew = (state.skew || 0) + 8 * 60")
    chrome.wait(f"{HAND} === 'handoff written · clears in 2 min'")
    chrome.js("state.skew += 60")
    chrome.wait(f"{HAND} === 'handoff written · clears shortly'")
    chrome.js("state.skew += 3600")  # the server is late: no minus minutes
    time.sleep(1.7)
    assert chrome.js(HAND) == "handoff written · clears shortly" and not api.list_started.is_set()
    home(chrome, api, url, handoff={"on": True, "phase": "written", "due": "<img src=x onerror=window.pwned=1>"})
    assert chrome.js(HAND) == "handoff written · clears shortly" and chrome.js("window.pwned") is None


def handoff_note(kind, _said, raw=""):
    """As the server keeps it: no finished sentence, only the reason or the step, under `body`."""
    return {"ts": time.time(), "pane": PANE, "status": "handoff-" + kind, "body": raw, "project": "fixture", "destructive": False}


def open_history(chrome, url, tab="notifications"):
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#history') !== null && document.querySelector('.card') !== null")
    chrome.click("#history")
    chrome.wait("document.querySelector('#sheet [data-tab=decisions]') !== null")
    if tab == "decisions":
        chrome.click("#sheet [data-tab=decisions]")


def test_handoff_refusal_reason_is_shown_as_text(api_fixture):
    chrome, api, url = api_fixture
    evil = "<img src=x onerror=window.pwned=1> tests are still red"
    api.notifications = [handoff_note("refused", "Not cleared. " + evil, evil), handoff_note("stopped", "Clearing stopped half way (/rename). Have a look at the session.", "/rename")]
    api.audit = [{"id": "h1", "ts": time.time(), "pane": PANE, "project": "fixture", "decision": "auto-handoff", "outcome": "<b onclick=window.pwned=2>odd</b>", "reason": "", "command": "", "destructive": False, "login": "ulak", "delivered": None},
                 {"id": "h2", "ts": time.time(), "pane": PANE, "project": "fixture", "decision": "auto-handoff", "outcome": "refused", "reason": evil, "command": "", "destructive": False, "login": "ulak", "delivered": None}]
    open_history(chrome, url)
    first, second = chrome.js("[...document.querySelectorAll('#sheet [data-panel=notifications] .card')].map(e => [e.querySelector('b').textContent, e.querySelector('div').textContent])")
    assert first == ["fixture · auto handoff", "Not cleared. " + evil] and second == ["fixture · auto handoff", "Clearing stopped half way (/rename). Have a look at the session."]
    chrome.click("#sheet [data-tab=decisions]")
    odd, refused = chrome.js("[...document.querySelectorAll('#sheet .audit span')].map(e => e.textContent)")
    assert odd == "Ulak · <b onclick=window.pwned=2>odd</b>" and refused == "Ulak · not cleared: " + evil
    assert chrome.js("[document.querySelector('#sheet img'), document.querySelector('#sheet [onclick]'), window.pwned ?? null]") == [None, None, None]


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_handoff_rows_in_both_languages_and_both_schemes(api_fixture, scheme):
    chrome, api, url = api_fixture
    chrome.cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}])
    bodies = {"written": "Handoff written. Unless you come back within 10 min, the conversation is cleared and carries on.", "cleaned": "Cleared; carrying on from the handoff note.",
              "refused": "Not cleared. mid-migration", "failed": "The handoff could not be written; the session is as it was.", "stopped": "Clearing stopped half way (/clear). Have a look at the session."}
    api.notifications = [handoff_note(kind, body, {"refused": "mid-migration", "stopped": "/clear"}.get(kind, "")) for kind, body in bodies.items()]
    outcomes = ["ask", "clear", "rename", "continue", "cancelled", "refused", "failed"]
    api.audit = [{"id": f"h{i}", "ts": time.time(), "pane": PANE, "project": "fixture", "decision": "auto-handoff", "outcome": o, "reason": "mid-migration" if o in ("refused", "failed") else "",
                  "command": "/clear" if o == "clear" else "", "destructive": False, "login": "ulak", "delivered": None} for i, o in enumerate(outcomes)]
    notes = "[...document.querySelectorAll('#sheet [data-panel=notifications] .card')].map(e => [e.querySelector('b').textContent, e.querySelector('div').textContent])"
    rows = "[...document.querySelectorAll('#sheet .audit span')].map(e => e.textContent)"
    open_history(chrome, url)
    assert chrome.js(notes) == [["fixture · auto handoff", body] for body in bodies.values()]
    assert chrome.js(rows) == ["Ulak · asked for the handoff", "Ulak · cleared", "Ulak · renamed", "Ulak · asked to carry on", "Ulak · handoff cancelled", "Ulak · not cleared: mid-migration", "Ulak · stopped: mid-migration"]
    chrome.js("localStorage.setItem('lang', 'tr')")
    open_history(chrome, url)
    assert chrome.js(notes) == [["fixture · otomatik devir", body] for body in (
        "Devir yazıldı. 10 dk içinde dönmezsen temizlenip devam edecek.", "Temizlendi, devir notundan devam ediyor.", "Kesilmedi. mid-migration",
        "Devir yazılamadı, oturum olduğu gibi duruyor.", "Temizleme yarıda kaldı (/clear). Oturuma bak.")]
    assert chrome.js(rows) == ["Ulak · devir istendi", "Ulak · temizlendi", "Ulak · ad verildi", "Ulak · devam istendi", "Ulak · devirden vazgeçildi", "Ulak · kesilmedi: mid-migration", "Ulak · yarıda kaldı: mid-migration"]
    words = (WEB / "i18n.js").read_text()
    en, tr = words.split("\n  tr: {")[0], words.split("\n  tr: {")[1].split("\n};")[0]
    new = set(re.findall(r"\b(ho[A-Z]\w*):", words))
    assert len(new) >= 20 and all(re.search(rf"\b{k}:", en) and re.search(rf"\b{k}:", tr) for k in new), sorted(new)
    # the switch and the way out read on both grounds
    api.cards = [card("idle", handoff={"on": True, "phase": "written", "due": time.time() + 400}, meters=meters())]
    open_first(chrome, url)
    chrome.wait(SHOWN + " && !document.querySelector('#handBar').hidden")
    assert chrome.js("matchMedia('(prefers-color-scheme: light)').matches") is (scheme == "light")
    assert chrome.js("getComputedStyle(document.querySelector('#handBar [data-handoff-cancel]')).color") == token(chrome, "--strong")
    chrome.click("#sMeta")
    chrome.wait(f"{SWITCH} !== null")
    assert chrome.js(f"[{SWITCH}.getAttribute('aria-checked'), getComputedStyle({SWITCH}.querySelector('i')).backgroundColor === '{token(chrome, '--seal')}', getComputedStyle({SWITCH}).opacity]") == ["true", True, "1"]
    chrome.cdp("Emulation.setEmulatedMedia", features=[])
