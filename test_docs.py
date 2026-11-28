"""The words in the README and SECURITY.md are checked against the code they describe."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
README = (ROOT / "README.md").read_text()
APP = (ROOT / "app.py").read_text()


def section(title):
    return README.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


def test_every_api_route_is_listed_in_the_readme():
    api = section("API")
    routes = set(re.findall(r'@app\.(?:get|post|put|delete)\("(/api/[^"]*)"\)', APP))
    assert len(routes) > 15

    def listed(route):
        if route.startswith("/api/push/"):
            return "/api/push/" in api
        short = route.replace("/api/sessions/{pane}/", ".../")
        return any(re.search(re.escape(name) + r"(?![\w/{-])", api) for name in (route, short))

    assert sorted(r for r in routes if not listed(r)) == []


def test_the_example_config_shows_every_key_the_server_reads():
    block = section("Configuration").split("```json\n", 1)[1].split("```", 1)[0]
    read = set(re.findall(r'(?:CONFIG|config)\.get\("([a-z_]+)"', APP))
    assert sorted(read - set(json.loads(block))) == []


def test_both_pages_name_everything_the_browser_keeps():
    kept = set()
    for path in (ROOT / "web").glob("*.js"):
        kept |= set(re.findall(r"(?:store|localStorage)\.(?:set|setItem)\(\s*'?([A-Za-z]+)", path.read_text()))
    kept.discard("k")  # the helper that does the writing, not a stored name
    words = {"draft": "drafts", "OUTBOX": "messages you sent", "pins": "pins", "chips": "quick replies", "lang": "language", "KEY": "theme", "idleOpen": "idle group"}
    assert kept == set(words), "the browser keeps something new: say so in README.md and SECURITY.md"
    for name in ("README.md", "SECURITY.md"):
        sentence = re.search(r"[^.\n]*[Tt]he browser keeps[^.\n]*\.", (ROOT / name).read_text())
        assert sentence, name
        assert [w for w in words.values() if w not in sentence[0]] == [], name


def test_the_readme_says_which_agents_get_folded_work_and_what_happens_to_a_photo():
    folds = next(line for line in README.splitlines() if "folds away" in line)
    assert "Claude Code" in folds and "Other agents" in folds
    attach = next(line for line in README.splitlines() if line.startswith("- **Attach files.**"))
    assert "JPEG" in attach and "HEIC" in attach and "strip_jpeg" in APP


def test_the_security_page_describes_the_checks_the_code_makes():
    import app
    import core
    page = (ROOT / "SECURITY.md").read_text()
    assert f"at most {app.MAX_SUBS} subscriptions" in page
    assert "Content Security Policy" in page and "script-src 'self'" in core.CSP and "'unsafe-inline'" not in core.CSP.split("script-src")[1].split(";")[0]
    assert "Inline styles are allowed" in page and "style-src 'self' 'unsafe-inline'" in core.CSP
    assert "⟨U+200B⟩" in page and core.show_hidden("\u200b") == "⟨U+200B⟩"
    assert "characters that cannot be seen" in APP and '"always allow" is refused by the server' in page
    for word in ("JPEG", "HEIC", "rotation"):
        assert word in page


def test_the_site_and_the_readme_say_the_same_day_and_the_readme_counts_the_code():
    day = re.search(r"README on (\d+) November 2026", README)[1]
    assert f"on {day} November 2026" in (ROOT / "site/index.html").read_text()
    assert f"{day} Kasım 2026" in (ROOT / "site/tr/index.html").read_text()
    files = ["app.py", "core.py", "chat.py", "herdr_client.py", *(f"web/{p.name}" for p in (ROOT / "web").iterdir() if p.suffix in (".js", ".css", ".html"))]
    lines = sum(len((ROOT / f).read_text().splitlines()) for f in files)
    said = int(re.search(r"about ([\d,]+) lines", README)[1].replace(",", ""))
    assert abs(lines - said) <= 250, f"the README says about {said} lines, the code has {lines}"


def test_the_security_page_gives_the_long_command_rule_with_the_numbers_the_code_uses():
    import core
    page = (ROOT / "SECURITY.md").read_text()
    rule = next(p for p in page.split("\n\n") if p.startswith("**A command that is not all there"))
    assert f"at least {core.FULL_MIN_SHOWN} characters" in rule and f"{core.FULL_MAX // 1024} KB" in rule
    for said in ("session record", "exactly one", "built again", '"always allow" is refused', "can only be denied", "not on the card"):
        assert said in rule, said
    shown = "x" * core.FULL_MIN_SHOWN
    call = lambda command: {"tool": "Bash", "command": command}
    assert core.full_command(shown, [call(shown + " tail")]) and not core.full_command(shown[1:], [call(shown + " tail")])
    assert not core.full_command(shown, [call(shown + " a"), call(shown + " b")]) and not core.full_command(shown, [call(shown + "y" * core.FULL_MAX)])


def test_the_security_page_gives_the_limits_of_a_choice_on_the_card():
    import core
    page = (ROOT / "SECURITY.md").read_text()
    rule = next(p for p in page.split("\n\n") if p.startswith("**A question is answered from the card"))
    ask = lambda n=4, label=40, question=160, **more: core.card_options({"kind": "choice", "question": "q" * question, "options": [{"n": i, "label": "l" * label} for i in range(1, n + 1)], **more})
    assert ask() and not ask(n=5) and not ask(n=1) and not ask(label=41) and not ask(question=161) and not ask(multi=True) and not ask(hidden=["U+200B"])
    for said in ("two to four", "40 characters", "160 characters", "three lines", "one choice", "fingerprint"):
        assert said in rule, said


def test_the_readme_names_what_this_release_added():
    import core
    feature = lambda start: next(line for line in README.splitlines() if line.startswith(f"- **{start}"))
    assert "earlier ones" in feature("Find what was said.")
    assert "Try again" in feature("A message that did not leave is not lost.") and "never sent again by itself" in feature("A message that did not leave")
    assert "12" in feature("Short answers of your own.") and "this phone" in feature("Short answers of your own.")
    slash = feature("The agent's own slash commands.")
    assert "opens a menu" in slash and "/exit" in slash and any(kind == "ends" and name == "/exit" for name, kind in core.BUILTIN_SLASH["claude"])
    room = feature("Know how much room is left.")
    assert "card" in room and "cold" in room and "subagents" in room
    assert "from the card" in feature("Answer questions and menus.")
    assert "session record" in next(line for line in README.splitlines() if line.startswith("- **Your yes is bound"))
