"""Chrome smoke tests against the static PWA and a loopback-only fake API."""

import re
import json
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


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
        self.messages = [{"role": "assistant", "text": "Example answer"}]
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
        elif self.path == "/api/sessions/fake%3Ap1/chat":
            if self.api.fail_chat:
                self.json({"detail": "Fixture chat unavailable"}, 503)
                self.api.chat_failed.set()
                return
            item = self.api.cards[0]
            self.json({
                "messages": self.api.messages, "source": "transcript", "prompt": self.api.approval,
                "status": item["status"], "session": item, "receipt": self.api.receipt,
            })
        elif self.path == "/api/sessions/fake%3Ap1/commands" and self.api.commands is not None:
            self.json({"commands": self.api.commands})
        elif self.path == "/api/sessions/fake%3Ap1/screen":
            self.json({"text": self.api.screen, "ts": time.time()})
        elif self.path == "/api/sessions/fake%3Ap1/changes":
            self.json(self.api.changes)
        elif self.path == "/api/audit":
            self.json({"items": self.api.audit})
        elif self.path == "/api/notifications":
            self.json({"items": []})
        elif self.path == "/api/pins":
            self.json({"pins": []})
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

    def do_POST(self):
        api = self.api
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/presence":
            api.presence.append(body)
            self.json({"ok": True})
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
            wait_for(port_ready, "Chrome's DevTools port")
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
            const tail = messages.querySelector('.msg').lastChild;
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
    chrome.wait("document.querySelectorAll('.card').length === 12 && document.getAnimations().every(a => a.animationName === 'pulse')")
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
    for name in ("index.html", "app.js", "i18n.js"):
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
        for ink in ("text", "strong", "muted", "zer", "lal-t", "ok"):
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


def test_list_has_faded_out_before_it_reaches_the_dock_buttons(api_fixture):
    chrome, api, url = api_fixture
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.card') !== null")
    assert chrome.js("document.querySelector('#newSession').getBoundingClientRect().top - document.querySelector('.dock').getBoundingClientRect().top") >= 20
    # the fade above the buttons must not swallow taps meant for the list
    assert chrome.js("(() => { const d = document.querySelector('.dock').getBoundingClientRect(); return !document.elementFromPoint(d.left + d.width / 2, d.top + 4).closest('.dock'); })()")


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
