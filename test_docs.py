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
    words = {"draft": "drafts", "pins": "pins", "lang": "language", "KEY": "theme", "idleOpen": "idle group"}
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
