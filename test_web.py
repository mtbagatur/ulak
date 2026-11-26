"""Chrome smoke tests against the static PWA and a loopback-only fake API."""

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
        self.messages = [{"role": "assistant", "text": "Example answer"}]
        self.approval = APPROVAL
        self.events = []
        self.requests = []
        self.replies = []
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
                "status": item["status"], "session": item,
            })
        elif self.path == "/api/pins":
            self.json({"pins": []})
        elif self.path == "/api/limits":
            self.json({"now": time.time(), "providers": []})
        elif self.path == "/api/config":
            self.json({"quick_label": ""})
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
        if self.path != "/api/sessions/fake%3Ap1/approve":
            self.send_error(404)
            return
        api = self.api
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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
def ulak(tmp_path):
    with open_chrome(tmp_path) as chrome, serve_fake_api() as (api, url):
        yield chrome, api, url


def open_first(chrome, url):
    chrome.cdp("Page.navigate", url=url)
    chrome.click(".card-open")
    chrome.wait("!document.querySelector('#session').hidden && document.querySelector('#messages .msg') !== null")


def test_inline_approval_ignores_second_keyboard_reply_while_request_is_pending(ulak):
    chrome, api, url = ulak
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


def test_inline_approval_displays_entire_wrapped_command_at_320_px(ulak):
    chrome, api, url = ulak
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


def test_destructive_approval_shows_command_and_focuses_dialog_before_sending(ulak):
    chrome, api, url = ulak
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


def test_changed_prompt_cannot_be_approved_by_old_confirmation_sheet(ulak):
    chrome, api, url = ulak
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


def test_delayed_approval_navigation_does_not_interrupt_another_pane(ulak):
    chrome, api, url = ulak
    open_first(chrome, url)
    chrome.js("""(() => {
        const schedule = window.setTimeout;
        window.setTimeout = (callback, delay, ...args) => {
            if (delay !== 450) return schedule(callback, delay, ...args);
            window.finishApprovalNavigation = () => callback(...args);
            return 0;
        };
    })()""")
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#toasts .ok') !== null")
    api.publish_session("working")
    chrome.wait("typeof window.finishApprovalNavigation === 'function'")
    chrome.js("go('fake:p2'); window.finishApprovalNavigation()")
    assert chrome.js("location.hash") == "#/s/fake:p2"


def test_unknown_after_reply_times_out_without_resending_or_leaving_chat(ulak):
    chrome, api, url = ulak
    open_first(chrome, url)
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#toasts .ok')?.textContent === 'Reply sent'")
    api.publish_session("unknown")
    chrome.wait("[...document.querySelectorAll('#toasts .err')].some(e => e.textContent.includes('could not verify a screen change'))")
    assert chrome.js("location.hash") == "#/s/fake:p1"
    assert len(api.replies) == len(api.requests) == 1


def test_approval_409_shows_error_and_refreshes_prompt(ulak):
    chrome, api, url = ulak
    chrome.cdp("Page.navigate", url=url + "#/s/" + PANE)
    chrome.wait("document.querySelector('#promptZone pre')?.textContent === 'pytest -q'")
    api.approval = OTHER_APPROVAL  # The backend sees the change before it is delivered to the browser.
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#toasts .err')?.textContent === 'The approval screen changed; refresh'")
    chrome.wait(f"document.querySelector('#promptZone pre')?.textContent === {json.dumps(OTHER_APPROVAL['details'])}")
    assert not api.replies


@pytest.mark.parametrize("width", [320, 375, 1280])
def test_unknown_state_and_long_labels_stay_visible(ulak, width):
    chrome, api, url = ulak
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
    chrome.click("#lang")
    assert chrome.js("document.querySelector('#summary').textContent") == "Durumu bilinmeyen oturum: 1"


def test_long_choice_prompt_scrolls_without_hiding_composer(ulak):
    chrome, api, url = ulak
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


def test_last_line_of_long_reply_is_reachable_above_composer(ulak):
    chrome, api, url = ulak
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


def test_chat_urls_render_as_safe_clickable_links(ulak):
    chrome, api, url = ulak
    api.messages = [{"role": "assistant", "text": "Read [the guide](https://example.test/guide), https://example.test/docs and [the PDF](/home/user/project/notes.pdf)."}]
    open_first(chrome, url)
    chrome.wait("document.querySelector('#messages a[href*=example]') !== null")
    assert chrome.js("document.querySelector('#messages a')?.getAttribute('href')") == "https://example.test/guide"
    assert chrome.js("document.querySelector('#messages a')?.rel") == "noopener noreferrer"
    assert chrome.js("new URL(document.querySelectorAll('#messages a')[2].href).searchParams.get('path')") == "/home/user/project/notes.pdf"


def test_slash_command_suggestions_filter_and_fill_composer(ulak):
    chrome, api, url = ulak
    open_first(chrome, url)
    chrome.js("(() => { const input=document.querySelector('#message'); input.value='/'; input.dispatchEvent(new Event('input',{bubbles:true})); })()")
    chrome.wait("!document.querySelector('#slashOptions').hidden")
    assert chrome.js("[...document.querySelectorAll('#slashOptions [data-slash-cmd]')].some(b => b.dataset.slashCmd === '/clear')")
    chrome.js("(() => { const input=document.querySelector('#message'); input.value='/com'; input.dispatchEvent(new Event('input',{bubbles:true})); })()")
    assert chrome.js("[...document.querySelectorAll('#slashOptions [data-slash-cmd]')].every(b => b.dataset.slashCmd.startsWith('/com'))")
    chrome.click("#slashOptions [data-slash-cmd='/compact']")
    assert chrome.js("document.querySelector('#message').value") == "/compact "


def test_offline_refresh_retains_cards_and_sse_reconnect_loads_snapshot(ulak):
    chrome, api, url = ulak
    api.cards = [card("working")]
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('.g-working .card') !== null")
    chrome.wait("document.querySelector('#conn').hidden")
    assert api.ready.wait(5)
    api.ready.clear()
    chrome.offline(True)
    api.stream_generation += 1
    chrome.click("#refresh")
    chrome.wait("!document.querySelector('#conn').hidden")
    assert chrome.js("document.querySelector('.g-working .proj').textContent") == "fixture"
    api.cards = [card("unknown", project="updated-while-offline")]
    chrome.offline(False)
    assert api.ready.wait(5)
    chrome.wait("document.querySelector('.g-unknown .proj')?.textContent === 'updated-while-offline'")
    assert chrome.js("document.querySelector('#conn').hidden")


def test_open_chat_retains_draft_and_waits_for_fresh_data_after_sse_ready(ulak):
    chrome, api, url = ulak
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


def test_sse_update_survives_a_late_list_snapshot(ulak):
    chrome, api, url = ulak
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#conn').hidden && document.querySelector('.g-blocked .card') !== null")
    api.list_started.clear()
    api.list_respond.clear()
    chrome.click("#refresh")
    assert api.list_started.wait(5)
    api.publish_session("unknown")
    chrome.wait("document.querySelector('.g-unknown .card') !== null")
    api.list_respond.set()
    chrome.wait("!document.querySelector('#refresh').classList.contains('loading')")
    assert chrome.js("document.querySelector('.g-unknown .card') !== null")
    assert chrome.js("document.querySelector('.g-blocked .card') === null")


def test_stalled_snapshot_recovers_without_reopening_the_stream(ulak):
    chrome, api, url = ulak
    chrome.cdp("Page.navigate", url=url)
    chrome.wait("document.querySelector('#conn').hidden && document.querySelector('.g-blocked .card') !== null")
    chrome.js("""(() => {
        const schedule = window.setTimeout;
        window.setTimeout = (callback, delay, ...args) => schedule(callback, delay === 15000 ? 150 : delay, ...args);
    })()""")
    api.ready.clear()
    api.list_started.clear()
    api.list_respond.clear()
    chrome.click("#refresh")
    assert api.list_started.wait(5)
    chrome.wait("!document.querySelector('#conn').hidden")
    api.cards = [card("unknown")]
    api.list_respond.set()
    chrome.wait("document.querySelector('#conn').hidden && document.querySelector('.g-unknown .card') !== null")
    assert not api.ready.is_set()
