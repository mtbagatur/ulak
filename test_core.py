from datetime import datetime
from zoneinfo import ZoneInfo
from core import NotificationGate, is_destructive, quiet_hours, status_rank
import asyncio, json
from herdr_client import HerdrClient
from fastapi.testclient import TestClient
import app as ulak


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

def test_tailscale_header_required(monkeypatch):
    monkeypatch.setattr(ulak, 'allowed_login', 'user@example.test')
    c=TestClient(ulak.app)
    assert c.get('/api/sessions').status_code == 403

def test_orphan_approval_rejected(monkeypatch):
    monkeypatch.setattr(ulak, 'allowed_login', 'user@example.test')
    c=TestClient(ulak.app)
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


# --- Review tests: real screens, fresh-read approval ---
from pathlib import Path
from core import parse_approval
CLAUDE = (Path(__file__).parent / "fixtures/claude-bash-approval.txt").read_text()
CODEX = """• I need to run a command outside the sandbox.

  Would you like to run the following command?

  $ touch ulak-probe2.txt

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command (a)
  3. No, and tell Codex what to do differently (esc)
"""

def test_claude_keys_from_options():
    p = parse_approval(CLAUDE)
    assert p["keys"] == {"approve": "1", "always": "2", "deny": "4"}
    assert "curl -s -X POST http://127.0.0.1:9/ulak-probe" in p["command"]
    assert "Ran 1 shell command" not in p["command"]

def test_codex_keys_from_options():
    p = parse_approval(CODEX)
    assert p["keys"] == {"approve": "1", "always": "2", "deny": "3"}
    assert "touch ulak-probe2.txt" in p["command"]

def test_no_prompt_no_approval():
    assert parse_approval("❯ 1. Yes this is just chat\nall done") is None
    assert parse_approval("") is None

def test_trust_dialog_not_an_approval():
    assert parse_approval("Is this a project you trust?\n ❯ No, exit\n   Yes, I trust this folder") is None


class FakeHerdr:
    def __init__(self, text): self.text, self.sent = text, []
    async def call(self, method, params=None):
        if method == "agent.read": return {"read": {"text": self.text}}
        if method == "agent.send_keys": self.sent.append(params["keys"]); return {"ok": True}
        return {}

def _setup(monkeypatch, text):
    fake = FakeHerdr(text)
    monkeypatch.setattr(ulak, "client", fake)
    monkeypatch.setattr(ulak, "allowed_login", "user@example.test")
    ulak.sessions.clear(); ulak.sessions["w9:p1"] = {"pane_id": "w9:p1", "status": "blocked"}
    ulak.pane_ids.add("w9:p1"); ulak.pending_confirm.clear()
    return fake, TestClient(ulak.app), {"Tailscale-User-Login": "user@example.test"}

def test_stale_fingerprint_rejected(monkeypatch):
    fake, c, h = _setup(monkeypatch, CODEX)
    r = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": "old"})
    assert r.status_code == 409 and fake.sent == []

def test_safe_approve_sends_option_key(monkeypatch):
    fake, c, h = _setup(monkeypatch, CODEX)
    fp = parse_approval(CODEX)["fingerprint"]
    assert c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fp}).status_code == 200
    assert fake.sent == [["1"]]

def test_destructive_needs_two_steps(monkeypatch):
    fake, c, h = _setup(monkeypatch, CLAUDE)  # curl -X POST is destructive
    fp = parse_approval(CLAUDE)["fingerprint"]
    r = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fp}).json()
    assert r["confirm_required"] and fake.sent == []
    assert c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "always", "fingerprint": fp}).status_code == 400
    ok = c.post("/api/sessions/w9:p1/approve", headers=h, json={"decision": "approve", "fingerprint": fp, "confirm_token": r["confirm_token"]})
    assert ok.status_code == 200 and fake.sent == [["1"]]

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
    assert p["kind"] == "trust" and "ulak-test" in p["question"]
    assert [o["keys"] for o in p["options"]] == [["Enter"], ["Down", "Enter"]]
    assert p["options"][1]["label"] == "Yes, I trust this folder"

def test_multiselect_digit_only_and_checked():
    p = parse_prompt(fx("claude-multiselect.txt"), "codex")
    assert p["multi"] and [o["checked"] for o in p["options"]] == [False, True, False]
    assert all(o["keys"] == [str(o["n"])] for o in p["options"])

def test_claude_bash_approval_is_approval_kind():
    p = parse_prompt(CLAUDE)
    assert p["kind"] == "approval" and "ulak-probe" in p["details"]
    assert [o["keys"] for o in p["options"]] == [["1"], ["2"], ["4"]]

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
    s = {"pane_id": "w9:p1", "status": "blocked", "cwd": "/home/x/projeler/ulak"}
    push = build_push(s, parse_prompt(CLAUDE), destructive=True)
    assert push["title"] == "ulak · needs you" and push["body"].startswith("⚠ ") and "curl -s -X POST" in push["body"]
    assert push["tag"] == "ulak-w9:p1" and push["url"] == "/?pane=w9:p1"

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
    assert project_name({"cwd": "/home/b/shared/panel/"}) == "panel"


def test_approval_details_are_tidied():
    p = parse_prompt(CLAUDE)
    assert "Tip:" not in p["details"] and "below" not in p["details"] and "requires approval" not in p["details"]
    assert p["details"].splitlines()[-1] == "curl -s -X POST http://127.0.0.1:9/ulak-probe"


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
    monkeypatch.setattr(ulak, "client", fake)
    monkeypatch.setattr(ulak, "allowed_login", "user@example.test")
    ulak.sessions.clear(); ulak.sessions["w9:p1"] = {"pane_id": "w9:p1", "status": "blocked", "agent": agent, "project": "x", "since": 0, "bucket": "blocked"}
    ulak.pane_ids.add("w9:p1"); ulak.pending_confirm.clear()
    return fake, TestClient(ulak.app), {"Tailscale-User-Login": "user@example.test"}

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
    ulak.sessions["wB:p1"] = {"pane_id": "wB:p1", "quick": True, "status": "idle"}
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
    monkeypatch.setattr(ulak, "STATE", tmp_path)
    monkeypatch.setattr(ulak, "allowed_login", "user@example.test")
    c = TestClient(ulak.app)
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
    monkeypatch.setattr(ulak, "STATE", tmp_path)
    r = c.post("/api/sessions/w9:p1/upload", headers={**h, "Content-Type": "image/png"}, content=b"\x89PNG-data")
    assert r.status_code == 200 and open(r.json()["path"], "rb").read() == b"\x89PNG-data" and r.json()["path"].endswith(".png")
    assert c.post("/api/sessions/w9:p1/upload", headers={**h, "Content-Type": "application/x-sh"}, content=b"x").status_code == 415
    assert c.post("/api/sessions/nope/upload", headers={**h, "Content-Type": "image/png"}, content=b"x").status_code == 404
    assert c.post("/api/sessions/w9:p1/upload", content=b"x", headers={"Content-Type": "image/png"}).status_code == 403


def test_limits_order_claude_codex_agy(monkeypatch, tmp_path):
    fake, c, h = _setup(monkeypatch, CODEX)
    f = tmp_path / "a.json"; f.write_text('{"h5": {"used_percentage": 1, "resets_at": 5}}')
    monkeypatch.setattr(ulak, "CONFIG", {"limit_files": {"agy": str(f), "claude": str(f)}})
    monkeypatch.setattr(ulak, "codex_limits", lambda: {"id": "codex", "label": "Codex", "updated": 1, "items": []})
    assert [p["id"] for p in c.get("/api/limits", headers=h).json()["providers"]] == ["claude", "codex", "agy"]


def test_tidy_details_keeps_only_the_command():
    block = "Bash command\n  git push origin main\n  Run shell command\nHook PreToolUse:Bash requires confirmation for this command:\n[x] Disari giden"
    from core import tidy_details
    assert tidy_details(block) == "git push origin main"


def test_codex_idle_composer_is_chrome():
    from chat import clean_lines
    screen = "› devam\n\n• Tamam.\n\n› Ask Codex to do anything\n\n  GPT-6-Luna default · ~/ortak/panel · Panel devri"
    assert clean_lines(screen) == ["› devam", "", "• Tamam."]


def test_tidy_details_boxed_hook_lines():
    from core import tidy_details
    block = "Bash command\n  Run shell command\n  │ git push origin main\n  │ Hook PreToolUse:Bash requires confirmation for this command:\n  │ [x] why"
    assert tidy_details(block) == "git push origin main"
