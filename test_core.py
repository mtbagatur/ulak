from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
from core import NotificationGate, is_destructive, quiet_hours, status_rank, bucket
import asyncio, json
from herdr_client import HerdrClient, HerdrError
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
    # Import with default settings so tests never depend on the live configuration.
    import app as api


def test_rm_is_destructive(): assert is_destructive("rm -rf ./build", "/missing")
def test_push_is_destructive(): assert is_destructive("git push origin main", "/missing")
def test_curl_post_is_destructive(): assert is_destructive("curl -X POST https://example.test", "/missing")
def test_ssh_is_destructive(): assert is_destructive("ssh host", "/missing")
def test_aws_is_destructive(): assert is_destructive("aws s3 rm bucket", "/missing")
def test_echo_is_normal(): assert not is_destructive("echo hello", "/missing")
def test_git_status_is_normal(): assert not is_destructive("git status", "/missing")
def test_guard_rule_loaded(tmp_path):
    f=tmp_path/'rules.md';f.write_text('- desen: `dangerous-operation`\n')
    assert is_destructive("dangerous-operation", str(f))

def test_guard_rules_accept_plain_pattern_alias_and_legacy_format(tmp_path):
    f = tmp_path / "rules.md"
    f.write_text("# deployment rules\n\nplain-danger\n- pattern: `alias-danger`\n- desen: `legacy-danger`\n")
    assert is_destructive("plain-danger", str(f))
    assert is_destructive("alias-danger", str(f))
    assert is_destructive("legacy-danger", str(f))
    assert not is_destructive("deployment rules", str(f))
def test_debounce():
    g=NotificationGate();assert g.allow('w1:p1','done',0);assert not g.allow('w1:p1','done',5);assert g.allow('w1:p1','done',11)
def test_debounce_per_pane_and_status():
    g=NotificationGate();assert g.allow('a','blocked',0);assert g.allow('b','blocked',0);assert g.allow('a','done',0)
def test_quiet_night(): assert quiet_hours(datetime(2026,11,20,23,0,tzinfo=ZoneInfo('Europe/Istanbul')))
def test_quiet_early_morning(): assert quiet_hours(datetime(2026,11,20,6,0,tzinfo=ZoneInfo('Europe/Istanbul')))
def test_daytime_allowed(): assert not quiet_hours(datetime(2026,11,20,12,0,tzinfo=ZoneInfo('Europe/Istanbul')))
def test_quick_target_not_special_in_order():
    assert status_rank({"workspace_label":"main","status":"working","since":1}) > status_rank({"workspace_label":"work","status":"working","since":2})
def test_blocked_before_working(): assert status_rank({'status':'blocked'}) < status_rank({'status':'working'})
def test_unknown_is_not_grouped_as_idle():
    assert bucket("unknown") == "unknown"
    assert status_rank({"bucket": "unknown"}) < status_rank({"bucket": "idle"})

def test_finished_session_moves_to_idle_after_five_minutes():
    now = 1_000_000
    assert bucket("done", now - 299, now) == "done"
    assert bucket("done", now - 300, now) == "idle"

def test_tailscale_header_required(monkeypatch):
    monkeypatch.setattr(api, 'allowed_login', 'user@example.test')
    c=TestClient(api.app)
    assert c.get('/api/sessions').status_code == 403

def test_orphan_approval_rejected(monkeypatch):
    monkeypatch.setattr(api, 'allowed_login', 'user@example.test')
    c=TestClient(api.app)
    r=c.post('/api/sessions/w404:p1/approve',headers={'Tailscale-User-Login':'user@example.test'},json={'decision':'approve'})
    assert r.status_code == 404

def test_fake_herdr_socket_rpc(tmp_path):
    async def scenario():
        path=str(tmp_path/'herdr.sock')
        async def serve(reader, writer):
            msg=json.loads(await reader.readline())
            writer.write((json.dumps({'id':msg['id'],'result':{'ok':True,'method':msg['method']}})+'\n').encode())
            await writer.drain();writer.close()
        server=await asyncio.start_unix_server(serve,path)
        async with server:
            client=HerdrClient(path)
            result=await client.call('agent.list')
            assert result=={'ok':True,'method':'agent.list'}
            await client.close()
    asyncio.run(scenario())

def test_passive_screen_reads_do_not_request_scrollback(monkeypatch):
    class ReadClient:
        def __init__(self): self.params = []
        async def call(self, method, params=None):
            self.params.append(params)
            return {"read": {"text": params["source"]}}

    async def scenario():
        fake = ReadClient()
        monkeypatch.setattr(api, "client", fake)
        api.read_cache.clear()
        assert await api.read_screen("w1:p1", "idle", force=True) == "visible"
        # A user-opened chat may inspect bounded recent history; it must not reuse
        # the passive snapshot cached for the session card.
        assert await api.read_screen("w1:p1", "idle", history=True) == "recent_unwrapped"
        assert await api.read_screen("w1:p1", "blocked", history=True, force=True) == "visible"
        assert [p["source"] for p in fake.params] == ["visible", "recent_unwrapped", "visible"]
        assert fake.params[1]["lines"] == 120
        api.read_cache.clear()

    asyncio.run(scenario())

def test_closed_pane_drops_ephemeral_state(monkeypatch):
    class CloseClient:
        async def subscribe(self, subscriptions):
            return None

    async def scenario():
        pane = "w99:p99"
        monkeypatch.setattr(api, "client", CloseClient())
        api.sessions[pane] = {"pane_id": pane}
        api.raw_agents[pane] = {"pane_id": pane}
        api.read_cache[pane] = (0, "stale", "idle", "visible")
        api.pending_confirm[pane] = {"token": "test"}
        api.pushed[pane] = "fingerprint"
        api.work_started[pane] = 1
        api.pane_ids.add(pane)

        await api.handle_event({"event": "pane.closed", "data": {"pane_id": pane}})

        assert pane not in api.sessions
        assert pane not in api.raw_agents
        assert pane not in api.read_cache
        assert pane not in api.pending_confirm
        assert pane not in api.pushed
        assert pane not in api.work_started
        assert pane not in api.pane_ids

    asyncio.run(scenario())


def test_initialize_subscribes_before_http_requests_without_loading_every_session(monkeypatch):
    async def scenario():
        calls = []
        class StartupClient:
            async def subscribe(self, subscriptions): calls.append(subscriptions)
        async def forbidden_load(): raise AssertionError("startup must not wait for every pane")
        async def idle_event_loop(): return None
        monkeypatch.setattr(api, "client", StartupClient())
        monkeypatch.setattr(api, "load_all", forbidden_load)
        monkeypatch.setattr(api, "event_loop", idle_event_loop)
        monkeypatch.setattr(api, "load_since", lambda: None)
        await asyncio.wait_for(api.initialize(), 0.1)
        await asyncio.sleep(0)
        assert calls == [api.BASE_EVENTS]

    asyncio.run(scenario())


def test_fresh_visible_presence_suppresses_push_but_hidden_or_stale_presence_does_not(monkeypatch):
    async def scenario():
        now = 1000.0
        monkeypatch.setattr(api, "sessions", {"w9:p1": {"pane_id": "w9:p1", "status": "done"}})
        monkeypatch.setattr(api, "client_presence", {"00000000-0000-4000-8000-000000000001": now}, raising=False)
        monkeypatch.setattr(api, "gate", type("Gate", (), {"allow": lambda self, *args: True})())
        monkeypatch.setattr(api.time, "time", lambda: now)
        async def no_wait(_): return None
        monkeypatch.setattr(api.asyncio, "sleep", no_wait)
        sends = []
        async def send(*args): sends.append(args)
        monkeypatch.setattr(api, "send_push", send)

        await api.push_after_grace("w9:p1", "done", 120)
        assert sends == []

        api.client_presence["00000000-0000-4000-8000-000000000001"] = now - 11
        await api.push_after_grace("w9:p1", "done", 120)
        assert len(sends) == 1


    asyncio.run(scenario())


def test_presence_endpoint_records_only_fresh_visible_tabs(monkeypatch):
    _, c, h = _setup(monkeypatch, CODEX)
    monkeypatch.setattr(api, "client_presence", {}, raising=False)
    client_id = "00000000-0000-4000-8000-000000000001"
    visible = c.post("/api/presence", headers=h, json={"client_id": client_id, "visible": True})
    assert visible.status_code == 200 and client_id in api.client_presence
    hidden = c.post("/api/presence", headers=h, json={"client_id": client_id, "visible": False})
    assert hidden.status_code == 200 and client_id not in api.client_presence
    invalid = c.post("/api/presence", headers=h, json={"client_id": "tab", "visible": True})
    assert invalid.status_code == 400


# --- Review tests: real screens, fresh-read approval ---
from core import parse_approval
CLAUDE = (Path(__file__).parent / "fixtures/claude-bash-approval.txt").read_text()
CODEX = """• I need to run a command outside the sandbox.

  Would you like to run the following command?

  $ touch api-probe2.txt

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command (a)
  3. No, and tell Codex what to do differently (esc)
"""

def test_claude_keys_from_options():
    p = parse_approval(CLAUDE)
    assert p["keys"] == {"approve": "1", "always": "2", "deny": "4"}
    assert "curl -s -X POST http://127.0.0.1:9/api-probe" in p["command"]
    assert "Ran 1 shell command" not in p["command"]

def test_codex_keys_from_options():
    p = parse_approval(CODEX)
    assert p["keys"] == {"approve": "1", "always": "2", "deny": "3"}
    assert "touch api-probe2.txt" in p["command"]

def test_no_prompt_no_approval():
    assert parse_approval("❯ 1. Yes this is just chat\nall done") is None
    assert parse_approval("") is None

def test_trust_dialog_not_an_approval():
    assert parse_approval("Is this a project you trust?\n ❯ No, exit\n   Yes, I trust this folder") is None


class FakeHerdr:
    def __init__(self, text): self.text, self.sent = text, []
    async def subscribe(self, subscriptions): return None
    async def call(self, method, params=None):
        if method == "agent.read": return {"read": {"text": self.text}}
        if method == "agent.send_keys": self.sent.append(params["keys"]); return {"ok": True}
        return {}

def _setup(monkeypatch, text):
    fake = FakeHerdr(text)
    monkeypatch.setattr(api, "client", fake)
    monkeypatch.setattr(api, "allowed_login", "user@example.test")
    monkeypatch.setattr(api, "sessions", {"w9:p1": {"pane_id": "w9:p1", "status": "blocked"}})
    monkeypatch.setattr(api, "pane_ids", {"w9:p1"})
    monkeypatch.setattr(api, "pending_confirm", {})
    return fake, TestClient(api.app), {"Tailscale-User-Login": "user@example.test"}

def test_stale_fingerprint_rejected(monkeypatch):
    fake, c, h = _setup(monkeypatch, CODEX)
    r = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": "old"})
    assert r.status_code == 409 and fake.sent == []

def test_safe_approve_sends_option_key(monkeypatch):
    fake, c, h = _setup(monkeypatch, CODEX)
    fp = parse_approval(CODEX)["fingerprint"]
    assert c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fp}).status_code == 200
    assert fake.sent == [["1"]]

def test_approval_fingerprint_changes_with_the_question_but_survives_color(monkeypatch):
    question = "Would you like to run the following command?"
    fake, client, headers = _setup(monkeypatch, CODEX)
    path = "/api/sessions/w9:p1/approve"
    action = {"decision": "approve", "fingerprint": parse_prompt(CODEX, "codex")["fingerprint"]}
    fake.text = CODEX.replace(question, "Would you like to allow the following command?")
    assert client.post(path, headers=headers, json=action).status_code == 409
    assert fake.sent == []
    fake.text = CODEX.replace(question, f"\033[1m{question}\033[0m")
    assert client.post(path, headers=headers, json=action).status_code == 200
    assert fake.sent == [["1"]]

def test_destructive_needs_two_steps(monkeypatch):
    fake, c, h = _setup(monkeypatch, CLAUDE)  # curl -X POST is destructive
    fp = parse_approval(CLAUDE)["fingerprint"]
    r = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fp}).json()
    assert r["confirm_required"] and fake.sent == []
    assert c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "always", "fingerprint": fp}).status_code == 400
    ok = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fp, "confirm_token": r["confirm_token"]})
    assert ok.status_code == 200 and fake.sent == [["1"]]

def test_destructive_confirmation_rejects_changed_prompt(monkeypatch):
    fake, c, h = _setup(monkeypatch, CLAUDE)
    fp = parse_approval(CLAUDE)["fingerprint"]
    first = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fp}).json()
    assert first["confirm_required"]

    fake.text = CLAUDE.replace("/api-probe", "/api-probe-changed")
    changed = c.post("/api/sessions/w9:p1/approve", headers=h, json={
        "decision": "approve", "fingerprint": fp, "confirm_token": first["confirm_token"]
    })
    assert changed.status_code == 409 and fake.sent == []

def test_destructive_confirmation_cannot_be_replayed(monkeypatch):
    fake, c, h = _setup(monkeypatch, CLAUDE)
    path = "/api/sessions/w9:p1/approve"
    action = {"decision": "approve", "fingerprint": parse_approval(CLAUDE)["fingerprint"]}
    first = c.post(path, headers=h, json=action).json()
    confirmed = {**action, "confirm_token": first["confirm_token"]}
    for token in ("stale", "geçersiz"):
        invalid = c.post(path, headers=h, json={**action, "confirm_token": token})
        assert invalid.status_code == 409 and fake.sent == []
    assert c.post(path, headers=h, json=confirmed).json()["ok"] is True

    assert c.post(path, headers=h, json=confirmed).status_code == 409
    assert fake.sent == [["1"]]

def test_deny_invalidates_confirmation_if_command_is_asked_again(monkeypatch):
    fake, client, headers = _setup(monkeypatch, CLAUDE)
    path = "/api/sessions/w9:p1/approve"
    action = {"decision": "approve", "fingerprint": parse_approval(CLAUDE)["fingerprint"]}
    first = client.post(path, headers=headers, json=action).json()
    assert client.post(path, headers=headers, json={**action, "decision": "deny"}).status_code == 200
    assert client.post(path, headers=headers, json={**action, "confirm_token": first["confirm_token"]}).status_code == 409
    assert fake.sent == [["4"]]


def test_observed_replacement_invalidates_confirmation_if_command_returns(monkeypatch):
    fake, client, headers = _setup(monkeypatch, CLAUDE)
    monkeypatch.setattr(api, "read_cache", {})
    path = "/api/sessions/w9:p1/approve"
    action = {"decision": "approve", "fingerprint": parse_approval(CLAUDE)["fingerprint"]}
    first = client.post(path, headers=headers, json=action).json()
    fake.text = CODEX
    assert client.get("/api/sessions/w9:p1/chat", headers=headers).status_code == 200
    fake.text = CLAUDE
    assert client.post(path, headers=headers, json={**action, "confirm_token": first["confirm_token"]}).status_code == 409
    assert fake.sent == []


def test_confirmation_does_not_apply_to_a_replacement_safe_prompt(monkeypatch):
    fake, c, h = _setup(monkeypatch, CLAUDE)
    path = "/api/sessions/w9:p1/approve"
    request = {"decision": "approve", "fingerprint": parse_approval(CLAUDE)["fingerprint"]}
    first = c.post(path, headers=h, json=request).json()

    fake.text = CODEX
    changed = c.post(path, headers=h, json={
        **request, "fingerprint": parse_approval(CODEX)["fingerprint"], "confirm_token": first["confirm_token"]
    })
    assert changed.status_code == 409 and fake.sent == []

def test_expired_confirmation_requires_a_fresh_request(monkeypatch):
    fake, c, h = _setup(monkeypatch, CLAUDE)
    path = "/api/sessions/w9:p1/approve"
    action = {"decision": "approve", "fingerprint": parse_approval(CLAUDE)["fingerprint"]}
    first = c.post(path, headers=h, json=action).json()
    api.pending_confirm["w9:p1"]["expires"] = 0
    assert c.post(path, headers=h, json={**action, "confirm_token": first["confirm_token"]}).status_code == 409
    assert fake.sent == []
    fresh = c.post(path, headers=h, json=action).json()
    assert fresh["confirm_required"] and fresh["confirm_token"] != first["confirm_token"]
    assert c.post(path, headers=h, json={**action, "confirm_token": fresh["confirm_token"]}).status_code == 200
    assert fake.sent == [["1"]]

def test_prompt_replies_are_exclusive_per_pane_while_in_flight(monkeypatch):
    class PausedHerdr(FakeHerdr):
        def __init__(self):
            super().__init__(CODEX)
            self.read_started = asyncio.Event()
            self.read_release = asyncio.Event()
            self.send_started = asyncio.Event()
            self.send_release = asyncio.Event()

        async def call(self, method, params=None):
            if params["target"] == "w9:p1":
                if method == "agent.read":
                    self.read_started.set()
                    await self.read_release.wait()
                if method == "agent.send_keys":
                    self.send_started.set()
                    await self.send_release.wait()
            return await super().call(method, params)

    async def scenario():
        _, _, headers = _setup(monkeypatch, CODEX)
        fake = PausedHerdr()
        monkeypatch.setattr(api, "client", fake)
        api.sessions["w9:p2"] = {"pane_id": "w9:p2", "status": "idle"}
        root = "/api/sessions/w9:p1"
        action = {"decision": "approve", "fingerprint": parse_approval(CODEX)["fingerprint"]}
        async with AsyncClient(transport=ASGITransport(api.app), base_url="http://testserver", headers=headers) as c:
            first = asyncio.create_task(c.post(f"{root}/approve", json=action))
            try:
                await asyncio.wait_for(fake.read_started.wait(), 3)
                assert (await asyncio.wait_for(c.post(f"{root}/approve", json=action), 3)).status_code == 409
                fake.read_release.set()
                await asyncio.wait_for(fake.send_started.wait(), 3)

                keys = await asyncio.wait_for(c.post(f"{root}/keys", json={"keys": ["enter"]}), 3)
                choice = await asyncio.wait_for(c.post(f"{root}/choose", json={"n": 1}), 3)
                assert keys.status_code == 409 and "in progress" in keys.json()["detail"]
                assert choice.status_code == 409 and "in progress" in choice.json()["detail"]

                assert (await c.post("/api/sessions/w9:p2/keys", json={"keys": ["esc"]})).status_code == 200
                assert fake.sent == [["esc"]]
            finally:
                fake.read_release.set()
                fake.send_release.set()
                response = await asyncio.wait_for(first, 3)
            assert response.status_code == 200 and fake.sent == [["esc"], ["1"]]

    asyncio.run(scenario())

def test_prompt_reply_can_be_retried_after_a_failed_rpc(monkeypatch):
    class FailingHerdr(FakeHerdr):
        fail = True

        async def call(self, method, params=None):
            if method == "agent.send_keys" and self.fail:
                self.fail = False
                raise HerdrError("fake transport error before sending keys")
            return await super().call(method, params)

    _, _, headers = _setup(monkeypatch, CODEX)
    fake = FailingHerdr(CODEX)
    monkeypatch.setattr(api, "client", fake)
    client = TestClient(api.app, raise_server_exceptions=False)
    path = "/api/sessions/w9:p1/approve"
    action = {"decision": "approve", "fingerprint": parse_approval(CODEX)["fingerprint"]}
    assert client.post(path, headers=headers, json=action).status_code == 500
    assert fake.sent == []
    assert client.post(path, headers=headers, json=action).status_code == 200
    assert fake.sent == [["1"]]

def test_destructive_deny_is_one_step(monkeypatch):
    fake, c, h = _setup(monkeypatch, CLAUDE)
    fp = parse_approval(CLAUDE)["fingerprint"]
    assert c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "deny", "fingerprint": fp}).status_code == 200
    assert fake.sent == [["4"]]


# --- v2 tests: transcript cleaning, choices, push payload, sorting ---
from chat import clean_transcript, screen_chat, preview_from_screen, detect_model, claude_chat, pretty_model
from core import parse_prompt, build_push, bucket, project_name
FX = Path(__file__).parent / "fixtures"
def fx(name): return (FX / name).read_text()

def test_clean_drops_chrome_and_collapses_blanks():
    out = clean_transcript(fx("claude-working-screen.txt"))
    assert "ctx 3%" not in out and "⏵⏵" not in out and "Cooking" not in out and "────" not in out and "╭" not in out
    assert "\n\n\n" not in out and not out.startswith("\n") and not out.endswith("\n")

def test_segment_roles():
    blocks = screen_chat(fx("claude-working-screen.txt"))
    roles = [b["role"] for b in blocks]
    assert "user" in roles and "assistant" in roles and "tool" in roles
    tool = next(b for b in blocks if b["role"] == "tool")
    assert "app.py" in tool["text"]
    assert blocks[-1]["role"] == "assistant" and "core.py" in blocks[-1]["text"]

def test_preview_is_last_assistant_line():
    assert preview_from_screen(fx("claude-working-screen.txt")) == "There are two Python files: app.py and core.py."

def test_detect_model():
    assert detect_model(fx("claude-working-screen.txt")) == "Sonnet 5.5"
    assert detect_model("\n  GPT-6-Luna medium · ~/api · title\n") == "GPT-6-Luna"
    assert detect_model("nothing here") == ""
    assert detect_model("accept-edits · Gemini 3.8 Flash · high") == "Gemini 3.8 Flash"
    assert pretty_model("claude-sonnet-5-5") == "Sonnet 5.5"

def test_ask_user_question_options_and_descriptions():
    p = parse_prompt(fx("claude-askuserquestion.txt"))
    assert p["kind"] == "choice" and p["header"] == "Database"
    assert p["question"] == "Which database should the service use?"
    assert [o["n"] for o in p["options"]] == [1, 2, 3, 4]
    assert "Relational" in p["options"][0]["desc"]
    assert p["options"][1]["keys"] == ["2"]  # Claude acts on the digit alone

def test_codex_choice_needs_enter_but_approval_keeps_digit():
    p = parse_prompt(fx("codex-approval.txt"), "codex")
    assert p["kind"] == "approval" and [o["role"] for o in p["options"]] == ["approve", "always", "deny"]
    generic = "Pick one\n› 1. Alpha\n  2. Beta\n"
    assert parse_prompt(generic, "codex")["options"][1]["keys"] == ["2", "Enter"]
    assert parse_prompt(generic, "claude")["options"][1]["keys"] == ["2"]

def test_trust_dialog_unnumbered_keys():
    p = parse_prompt(fx("claude-trust.txt"))
    assert p["kind"] == "trust" and "api-test" in p["question"]
    assert [o["keys"] for o in p["options"]] == [["Enter"], ["Down", "Enter"]]
    assert p["options"][1]["label"] == "Yes, I trust this folder"

def test_multiselect_digit_only_and_checked():
    p = parse_prompt(fx("claude-multiselect.txt"), "codex")
    assert p["multi"] and [o["checked"] for o in p["options"]] == [False, True, False]
    assert all(o["keys"] == [str(o["n"])] for o in p["options"])

def test_claude_bash_approval_is_approval_kind():
    p = parse_prompt(CLAUDE)
    assert p["kind"] == "approval" and "api-probe" in p["details"]
    assert [o["keys"] for o in p["options"]] == [["1"], ["2"], ["4"]]

def test_agy_file_edit_is_an_approval():
    p = parse_prompt(fx("agy-file-edit-approval.txt"), "agy")
    assert p["kind"] == "approval"
    assert [o["role"] for o in p["options"]] == ["approve", "deny"]
    assert [o["keys"] for o in p["options"]] == [["1"], ["2"]]
    assert "accept-edits · Gemini" not in " ".join(b["text"] for b in screen_chat(fx("agy-file-edit-approval.txt")))

def test_agy_file_edit_overrides_terminal_done_status(monkeypatch):
    async def fake_read_screen(*args, **kwargs):
        return fx("agy-file-edit-approval.txt")
    monkeypatch.setattr(api, "read_screen", fake_read_screen)
    card = asyncio.run(api.make_card({"pane_id": "agy-test:p1", "agent": "agy", "agent_status": "done", "cwd": "/tmp/api-test"}))
    assert card["status"] == card["bucket"] == "blocked"
    assert card["pending"]["kind"] == "approval"
    assert card["model"] == "Gemini 3.8 Flash"

def test_mention_popup_is_dismiss_only():
    p = parse_prompt("All Results  Filesystem Only  Plugins\n 1. a\n 2. b")
    assert p["kind"] == "dismiss" and p["options"] == []

def test_plain_screen_has_no_prompt():
    assert parse_prompt(fx("claude-working-screen.txt")) is None

def test_prompt_fingerprint_changes_with_options():
    a = parse_prompt(fx("claude-askuserquestion.txt"))
    b = parse_prompt(fx("claude-askuserquestion.txt").replace("SQLite", "MySQL"))
    assert a["fingerprint"] != b["fingerprint"]

def test_push_blocked_payload_is_human():
    s = {"pane_id": "w9:p1", "status": "blocked", "cwd": "/home/user/api"}
    push = build_push(s, parse_prompt(CLAUDE), destructive=True)
    assert push["title"] == "api · needs you" and push["body"].startswith("⚠ ") and "curl -s -X POST" in push["body"]
    assert push["tag"].endswith("w9:p1") and push["url"] == "/?pane=w9:p1"

def test_push_done_uses_preview():
    push = build_push({"pane_id": "w1:p1", "status": "done", "cwd": "/a/api", "preview": "Devir yazıldı."})
    assert push["title"] == "api · done" and push["body"] == "Devir yazıldı."
    tr = build_push({"pane_id": "w1:p1", "status": "done", "cwd": "/a/api"}, lang="tr")
    assert tr["title"] == "api · bitti" and tr["body"] == "İş bitti" and tr["raw"] == ""

def test_sort_buckets_and_done_aging():
    now = 100000
    assert bucket("done", now - 60, now) == "done" and bucket("done", now - 7200, now) == "idle"
    rows = [
        {"status": "idle", "bucket": "idle", "workspace_label": "a"},
        {"status": "done", "bucket": "done", "workspace_label": "b"},
        {"status": "working", "bucket": "working", "workspace_label": "c"},
        {"status": "blocked", "bucket": "blocked", "workspace_label": "d"},
        {"status": "working", "bucket": "working", "workspace_label": "main"},
    ]
    order = [r["workspace_label"] for r in sorted(rows, key=status_rank)]
    assert order == ["d", "c", "main", "b", "a"]

def test_claude_jsonl_chat(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [
        {"type": "user", "promptSource": "typed", "message": {"role": "user", "content": "merhaba"}},
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "skill body"}},
        {"type": "assistant", "message": {"model": "claude-sonnet-5-5", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Selam"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a\nb"}]}},
        {"type": "user", "message": {"role": "user", "content": "<task-notification>x</task-notification>"}},
    ]
    f.write_text("\n".join(json.dumps(l) for l in lines))
    msgs, model = claude_chat(f)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[2]["text"] == "ls -la" and msgs[2]["result"] == "a\nb" and model == "Sonnet 5.5"

def test_project_name():
    assert project_name({"cwd": "/home/user/api/"}) == "api"


def test_approval_details_are_tidied():
    p = parse_prompt(CLAUDE)
    assert "Tip:" not in p["details"] and "below" not in p["details"] and "requires approval" not in p["details"]
    assert p["details"].splitlines()[-1] == "curl -s -X POST http://127.0.0.1:9/api-probe"


def test_live_ask_user_question_with_rule_inside_menu():
    p = parse_prompt(fx("claude-askuserquestion-live.txt"))
    assert p["kind"] == "choice" and p["header"] == "Database" and [o["n"] for o in p["options"]] == [1, 2, 3, 4]
    assert "relational" in p["options"][0]["desc"] and p["question"] == "Which database should we use?"


def test_live_trust_dialog_keys():
    p = parse_prompt(fx("claude-trust-live.txt"))
    assert p["kind"] == "trust" and [o["keys"] for o in p["options"]] == [["Enter"], ["Down", "Enter"]]


# --- v2 endpoint tests (fake herdr, no real pane touched) ---
ASK = (Path(__file__).parent / "fixtures/claude-askuserquestion-live.txt").read_text()

class FakeHerdr2(FakeHerdr):
    def __init__(self, text): super().__init__(text); self.calls = []
    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "workspace.create": return {"root_pane": {"pane_id": "w7:p1"}}
        return await super().call(method, params)

def _setup2(monkeypatch, text, agent="claude"):
    fake = FakeHerdr2(text)
    monkeypatch.setattr(api, "client", fake)
    monkeypatch.setattr(api, "allowed_login", "user@example.test")
    card = {"pane_id": "w9:p1", "status": "blocked", "agent": agent, "project": "x", "since": 0, "bucket": "blocked"}
    monkeypatch.setattr(api, "sessions", {"w9:p1": card})
    monkeypatch.setattr(api, "pane_ids", {"w9:p1"})
    monkeypatch.setattr(api, "pending_confirm", {})
    return fake, TestClient(api.app), {"Tailscale-User-Login": "user@example.test"}

def test_choose_sends_option_keys(monkeypatch):
    fake, c, h = _setup2(monkeypatch, ASK)
    fp = parse_prompt(ASK)["fingerprint"]
    assert c.post("/api/sessions/w9:p1/choose", headers=h, json={"fingerprint": fp, "n": 2}).status_code == 200
    assert fake.sent == [["2"]]

def test_choose_rejects_stale_or_unknown(monkeypatch):
    fake, c, h = _setup2(monkeypatch, ASK)
    fp = parse_prompt(ASK)["fingerprint"]
    assert c.post("/api/sessions/w9:p1/choose", headers=h, json={"fingerprint": "old", "n": 1}).status_code == 409
    assert c.post("/api/sessions/w9:p1/choose", headers=h, json={"fingerprint": fp, "n": 9}).status_code == 409
    assert fake.sent == []

def test_choose_rechecks_the_question_even_if_options_are_unchanged(monkeypatch):
    fake, c, h = _setup2(monkeypatch, ASK)
    monkeypatch.setattr(api, "read_cache", {})
    path = "/api/sessions/w9:p1"
    before = c.get(f"{path}/chat", headers=h).json()["prompt"]
    fake.text = ASK.replace("Which database should we use?", "Which database should we keep?")
    changed = c.post(f"{path}/choose", headers=h, json={"fingerprint": before["fingerprint"], "n": 2})

    assert changed.status_code == 409
    assert fake.sent == []
    assert [params["source"] for method, params in fake.calls if method == "agent.read"] == ["visible", "visible"]

def test_unknown_status_remains_visible_and_rejects_old_approval(monkeypatch, tmp_path):
    class SnapshotHerdr(FakeHerdr2):
        async def call(self, method, params=None):
            if method == "agent.list":
                return {"agents": [
                    {"pane_id": "w9:p2", "agent": "claude", "agent_status": "idle"},
                    {"pane_id": "w9:p1", "agent": "codex", "agent_status": "unknown"},
                ]}
            return await super().call(method, params)

    _, c, h = _setup2(monkeypatch, CODEX, "codex")
    fake = SnapshotHerdr(CODEX)
    monkeypatch.setattr(api, "client", fake)
    monkeypatch.setattr(api, "STATE", tmp_path)
    for name in ("raw_agents", "read_cache", "since"):
        monkeypatch.setattr(api, name, {})
    monkeypatch.setattr(api.refresh_pane, "labels", {}, raising=False)
    fingerprint = parse_approval(CODEX)["fingerprint"]

    cards = c.get("/api/sessions", headers=h).json()
    assert [(card["pane_id"], card["bucket"]) for card in cards] == [("w9:p1", "unknown"), ("w9:p2", "idle")]
    assert cards[0]["status"] == "unknown" and cards[0]["pending"] is None
    answer = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fingerprint})
    assert answer.status_code == 409 and fake.sent == []

def test_choose_refuses_approval_screens(monkeypatch):
    fake, c, h = _setup2(monkeypatch, CODEX, "codex")
    r = c.post("/api/sessions/w9:p1/choose", headers=h, json={"fingerprint": "x", "n": 1})
    assert r.status_code == 409 and fake.sent == []

def test_keys_whitelist(monkeypatch):
    fake, c, h = _setup2(monkeypatch, ASK)
    assert c.post("/api/sessions/w9:p1/keys", headers=h, json={"keys": ["rm -rf"]}).status_code == 400
    assert c.post("/api/sessions/w9:p1/keys", headers=h, json={"keys": ["Down", "Enter"]}).status_code == 200
    assert fake.sent == [["Down", "Enter"]]

def test_keybar_cannot_bypass_destructive_two_step(monkeypatch):
    fake, c, h = _setup2(monkeypatch, CLAUDE)  # destructive curl -X POST
    for keys in (["Enter"], ["1"], ["y"]):
        assert c.post("/api/sessions/w9:p1/keys", headers=h, json={"keys": keys}).status_code == 409
    assert c.post("/api/sessions/w9:p1/keys", headers=h, json={"keys": ["esc"]}).status_code == 200
    assert fake.sent == [["esc"]]

def test_close_is_two_step(monkeypatch):
    fake, c, h = _setup2(monkeypatch, ASK)
    r = c.post("/api/sessions/w9:p1/close", headers=h, json={}).json()
    assert r["confirm_required"] and not any(m == "pane.close" for m, _ in fake.calls)
    assert c.post("/api/sessions/w9:p1/close", headers=h, json={"confirm_token": "wrong"}).json().get("confirm_required")
    new = c.post("/api/sessions/w9:p1/close", headers=h, json={}).json()
    assert c.post("/api/sessions/w9:p1/close", headers=h, json={"confirm_token": new["confirm_token"]}).json() == {"ok": True}
    assert ("pane.close", {"pane_id": "w9:p1"}) in fake.calls

def test_start_session_validates_and_runs_fixed_command(monkeypatch, tmp_path):
    fake, c, h = _setup2(monkeypatch, "")
    home = Path.home() / "projeler"
    assert c.post("/api/sessions", headers=h, json={"cwd": "/etc", "tool": "claude", "model": ""}).status_code == 400
    assert c.post("/api/sessions", headers=h, json={"cwd": str(home), "tool": "claude", "model": "x; rm -rf ~"}).status_code == 400
    assert c.post("/api/sessions", headers=h, json={"cwd": str(home), "tool": "bash", "model": ""}).status_code == 400
    r = c.post("/api/sessions", headers=h, json={"cwd": str(home), "tool": "claude", "model": "haiku"})
    assert r.status_code == 200 and r.json()["pane"] == "w7:p1"
    assert ("pane.send_text", {"pane_id": "w7:p1", "text": "claude --model haiku"}) in fake.calls

def test_quick_send(monkeypatch):
    fake, c, h = _setup2(monkeypatch, "")
    assert c.post("/api/quick", headers=h, json={"text": "selam"}).status_code == 404
    api.sessions["wB:p1"] = {"pane_id": "wB:p1", "quick": True, "status": "idle"}
    assert c.post("/api/quick", headers=h, json={"text": "selam"}).status_code == 200
    assert ("agent.prompt", {"target": "wB:p1", "text": "selam"}) in fake.calls

def test_cut_claude_command_is_deny_only(monkeypatch):
    cut = "rm -rf build\n  Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and don't ask again for: rm *\n   3. No\n"
    fake, c, h = _setup2(monkeypatch, cut)
    p = parse_approval(cut)
    assert p and p["cut"]
    assert c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": p["fingerprint"]}).status_code == 409
    assert c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "deny", "fingerprint": p["fingerprint"]}).status_code == 200


def test_pins_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "STATE", tmp_path)
    monkeypatch.setattr(api, "allowed_login", "user@example.test")
    c = TestClient(api.app)
    h = {"Tailscale-User-Login": "user@example.test"}
    assert c.get("/api/pins", headers=h).json() == {"pins": []}
    assert c.put("/api/pins", headers=h, json={"pins": ["a|b", "a|b", "c|d"]}).json() == {"pins": ["a|b", "c|d"]}
    assert c.get("/api/pins", headers=h).json() == {"pins": ["a|b", "c|d"]}
    assert c.put("/api/pins", headers=h, json={"pins": [1]}).status_code == 400
    assert c.get("/api/pins").status_code == 403


def test_parse_limits_json_and_text():
    from core import parse_limits
    claude = parse_limits("claude", '{"h5": {"used_percentage": 4, "resets_at": 100}, "d7": {"used_percentage": 16.4, "resets_at": 200}, "ts": 50}')
    assert claude["label"] == "Claude" and claude["updated"] == 50
    assert [(i["label"], i["used"], i["resets_at"]) for i in claude["items"]] == [("5h", 4, 100), ("7d", 16, 200)]
    text = "Gemini Models\tWeekly Limit Remaining\t82%\t2026-11-25T13:17:24Z\nClaude and GPT models\tFive Hour Limit Remaining\tdisabled\t\n"
    agy = parse_limits("agy", text, 7)
    assert agy["updated"] == 7 and len(agy["items"]) == 1
    assert agy["items"][0]["label"] == "Gemini Models · 7d" and agy["items"][0]["used"] == 18 and agy["items"][0]["resets_at"] > 1e9
    assert parse_limits("x", "nonsense") is None

def test_dismiss_and_trust_prompts_carry_no_localised_text():
    trust = parse_prompt(open("fixtures/claude-trust.txt").read())
    assert trust["kind"] == "trust" and trust["header"] == "" and not any(ord(c) > 127 for c in trust["question"].replace("~", ""))


def test_codex_limits_read_newest_rate_limits():
    from core import parse_codex_limits
    line = '{"payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":2.0,"window_minutes":300,"resets_at":100},"secondary":{"used_percent":9.4,"window_minutes":10080,"resets_at":200}}}}'
    p = parse_codex_limits(["noise", line, '{"payload":{"rate_limits":null}}'], 5)
    assert p["id"] == "codex" and [(i["label"], i["used"]) for i in p["items"]] == [("5h", 2), ("7d", 9)]
    assert parse_codex_limits(["nothing"]) is None


def test_tool_models_adds_discovered_and_validates(monkeypatch):
    import app as appmod
    appmod._models_cache.clear()
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "m-one\tOne\nbad slug;rm\tX\n"})())
    m = appmod.tool_models("agy")
    assert m["m-one"] == " --model m-one" and "" in m and not any(";" in k for k in m)


def test_upload_saves_file_and_rejects_bad_types(monkeypatch, tmp_path):
    fake, c, h = _setup(monkeypatch, CODEX)
    monkeypatch.setattr(api, "STATE", tmp_path)
    r = c.post("/api/sessions/w9:p1/upload", headers={**h, "Content-Type": "image/png"}, content=b"\x89PNG-data")
    assert r.status_code == 200 and open(r.json()["path"], "rb").read() == b"\x89PNG-data" and r.json()["path"].endswith(".png")
    assert c.post("/api/sessions/w9:p1/upload", headers={**h, "Content-Type": "application/x-sh"}, content=b"x").status_code == 415
    assert c.post("/api/sessions/nope/upload", headers={**h, "Content-Type": "image/png"}, content=b"x").status_code == 404
    assert c.post("/api/sessions/w9:p1/upload", content=b"x", headers={"Content-Type": "image/png"}).status_code == 403


def test_session_file_route_serves_documents_within_workspace_only(monkeypatch, tmp_path):
    fake, c, h = _setup(monkeypatch, CODEX)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("# Notes")
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    monkeypatch.setattr(api, "raw_agents", {"w9:p1": {"pane_id": "w9:p1", "cwd": str(workspace)}})
    response = c.get("/api/sessions/w9:p1/file", headers=h, params={"path": "notes.md"})
    assert response.status_code == 200 and response.text == "# Notes"
    assert response.headers["content-disposition"].startswith("inline")
    assert c.get("/api/sessions/w9:p1/file", headers=h, params={"path": str(outside)}).status_code == 404
    assert c.get("/api/sessions/w9:p1/file", headers=h, params={"path": "missing.exe"}).status_code == 415


def test_limits_order_claude_codex_agy(monkeypatch, tmp_path):
    fake, c, h = _setup(monkeypatch, CODEX)
    f = tmp_path / "a.json"; f.write_text('{"h5": {"used_percentage": 1, "resets_at": 5}}')
    monkeypatch.setattr(api, "CONFIG", {"limit_files": {"agy": str(f), "claude": str(f)}})
    monkeypatch.setattr(api, "codex_limits", lambda: {"id": "codex", "label": "Codex", "updated": 1, "items": []})
    assert [p["id"] for p in c.get("/api/limits", headers=h).json()["providers"]] == ["claude", "codex", "agy"]


def test_tidy_details_keeps_only_the_command():
    block = "Bash command\n  git push origin main\n  Run shell command\nHook PreToolUse:Bash requires confirmation for this command:\n[x] Disari giden"
    from core import tidy_details
    assert tidy_details(block) == "git push origin main"


def test_codex_idle_composer_is_chrome():
    from chat import clean_lines
    screen = "› devam\n\n• Tamam.\n\n› Ask Codex to do anything\n\n  GPT-6-Luna default · /home/user/api · API handoff"
    assert clean_lines(screen) == ["› devam", "", "• Tamam."]


def test_tidy_details_boxed_hook_lines():
    from core import tidy_details
    block = "Bash command\n  Run shell command\n  │ git push origin main\n  │ Hook PreToolUse:Bash requires confirmation for this command:\n  │ [x] why"
    assert tidy_details(block) == "git push origin main"


def test_hook_explanation_is_extracted_as_single_line():
    from core import hook_note
    block = "Bash command\n  git push origin main\nHook PreToolUse:Bash requires confirmation for this command:\n[x] This sends changes to the remote repository.\nMore explanation"
    assert hook_note(block) == "This sends changes to the remote repository."
