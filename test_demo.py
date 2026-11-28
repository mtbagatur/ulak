import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from test_web import APPROVE, open_chrome
from tools import demo_server as demo


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def site(tmp_path):
    """The static build behind a plain file server, under a subpath, the way a web host would serve it."""
    demo.build(tmp_path / "site" / "demo")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(tmp_path / "site")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with open_chrome(tmp_path) as chrome:
            chrome.resize(390, 780)
            chrome.cdp("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/demo/?speed=8")
            chrome.wait("document.querySelectorAll('.card').length === 6")
            yield chrome
    finally:
        server.shutdown()


def open_card(chrome, pane):
    chrome.click(f".card[data-key={json.dumps(pane)}] .card-open")
    chrome.wait(f"location.hash === {json.dumps('#/s/' + pane)} && document.querySelector('#messages .msg') !== null")


def text(chrome, selector):
    return chrome.js(f"document.querySelector({json.dumps(selector)})?.textContent || ''")


def test_static_demo_needs_no_server_and_installs_nothing(site):
    chrome = site
    assert text(chrome, ".top h1").endswith("demo")
    assert chrome.js("document.querySelector('link[rel=manifest]') === null")
    assert chrome.js("performance.getEntriesByType('resource').filter(r => new URL(r.name).pathname.startsWith('/api')).length") == 0
    assert chrome.js("performance.getEntriesByType('resource').every(r => new URL(r.name).pathname.startsWith('/demo/'))")


def test_demo_story_approve_then_two_seals_then_the_agent_resumes(site):
    chrome = site
    open_card(chrome, "d1:p1")
    assert "retry_with_backoff" in text(chrome, "#messages")
    chrome.click(APPROVE)
    chrome.wait("location.hash === '#/s/d2:p1'")  # the next session that waits
    chrome.wait("document.querySelector('#promptZone .two-seals') !== null")
    assert "release/2.4" in text(chrome, "#messages") and "retry_with_backoff" not in text(chrome, "#messages")
    chrome.click(APPROVE)
    chrome.click("#sheet [data-y]")
    chrome.wait("document.querySelector('#receipt.ok') !== null")
    assert chrome.js("location.hash") == "#/s/d2:p1"  # nothing else waits, so the road to the agent stays in view
    chrome.wait("document.querySelector('#messages .hint')?.textContent.includes('Pushing')")
    chrome.wait("document.querySelector('#messages').textContent.includes('Pushed. CI is running')")
    chrome.js("history.back()")
    chrome.wait("document.querySelector('.card[data-key=\"d1:p1\"]').textContent.includes('All 12 sync tests pass')")
    assert "`" not in text(chrome, '.card[data-key="d1:p1"]')  # a card shows plain words, not markdown marks


def test_a_file_link_in_the_demo_opens_the_fake_file_not_the_host(site):
    chrome = site
    open_card(chrome, "d2:p1")
    href = chrome.js("""(() => { const a = document.querySelector('#messages a[href*="/file?path="]');
        a.addEventListener('click', e => e.preventDefault()); a.click(); return a.href; })()""")
    assert href.startswith("blob:"), href
    chrome.js(f"void fetch({json.dumps(href)}).then(r => r.text()).then(body => {{ window.fileBody = body; }})")
    chrome.wait("(window.fileBody || '').includes('Saved filters')")


def test_demo_refuses_an_answer_to_a_question_that_changed(site):
    chrome = site
    open_card(chrome, "d1:p1")
    chrome.click(APPROVE)
    chrome.wait("location.hash === '#/s/d2:p1' && document.querySelector('#promptZone .two-seals') !== null")
    chrome.click('#promptZone [data-d="deny"]')
    chrome.wait("document.querySelector('#messages').textContent.includes('nothing was pushed')")
    chrome.js("history.back()")
    chrome.wait("document.querySelector('.card[data-key=\"d3:p1\"] [data-act=approve]') !== null")  # once the queue is empty a third agent stops
    open_card(chrome, "d3:p1")
    assert text(chrome, "#promptZone pre") == "terraform plan -out=tf.plan"
    chrome.click(APPROVE)
    chrome.wait("document.querySelector('#toasts .err')?.textContent.includes('changed')")
    chrome.wait("document.querySelector('#promptZone pre')?.textContent === 'terraform apply tf.plan'")
    assert chrome.js("document.querySelector('#promptZone .two-seals') !== null")
    chrome.js("history.back()")
    chrome.click("#history")
    chrome.click("#sheet [data-tab=decisions]")
    assert "terraform plan -out=tf.plan" in text(chrome, "#sheet [data-panel=decisions]")


def test_demo_never_serves_a_stale_app():
    client = TestClient(demo.app)
    for path in ("/", "/static/app.js", "/static/i18n.js", "/static/md.js", "/static/demo.js"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_back_after_moving_to_the_next_waiting_session_goes_home(site):
    chrome = site
    open_card(chrome, "d1:p1")
    chrome.click(APPROVE)
    chrome.wait("location.hash === '#/s/d2:p1' && document.querySelector('#promptZone .two-seals') !== null")
    chrome.click("#back")  # not back into the session that was just answered
    chrome.wait("!document.querySelector('#home').hidden && location.hash === ''")


def test_demo_decisions_include_a_menu_choice(site):
    chrome = site
    chrome.click("#history")
    chrome.click("#sheet [data-tab=decisions]")
    row = "#sheet [data-panel=decisions] .audit"
    chrome.wait(f"document.querySelector({json.dumps(row)}) !== null")
    shown = text(chrome, "#sheet [data-panel=decisions]")
    assert "Chosen" in shown and "Which month should the backfill start from?" in shown and "> March 2026" in shown
    assert chrome.js("[...document.querySelectorAll('#sheet, #sheet .audit')].every(el => el.scrollWidth <= el.clientWidth)")


def test_demo_release_session_has_changes_to_show(site):
    chrome = site
    open_card(chrome, "d2:p1")
    chrome.click("#sMenu")
    chrome.click("#sheet [data-d]")
    chrome.wait("document.querySelector('#sheet .diff') !== null")
    shown = text(chrome, "#sheet")
    assert "release/2.4" in shown and "docs/releases/2.4.md" in shown and '"version": "2.4.0"' in shown


def test_demo_agents_speak_turkish_on_the_turkish_demo(site):
    chrome = site
    chrome.cdp("Page.navigate", url=chrome.js("location.origin + '/demo/?speed=8&lang=tr'"))
    chrome.wait("document.documentElement.lang === 'tr' && document.querySelectorAll('.card').length === 6")
    cards = text(chrome, "#home")
    assert "Kararsız senkron testini düzelt" in cards and "Doldurma bitti: 1,2 milyon satır" in cards
    assert "Fix flaky" not in cards and "Backfill finished" not in cards
    open_card(chrome, "d1:p1")
    shown = text(chrome, "#messages")
    assert "senkron testleri" in shown and "retry_with_backoff" in shown and "The sync test fails" not in shown
    chrome.click(APPROVE)
    chrome.wait("location.hash === '#/s/d2:p1' && document.querySelector('#messages').textContent.includes('Sürüm notları')")
    chrome.js("history.back()")
    chrome.wait("document.querySelector('.card[data-key=\"d1:p1\"]').textContent.includes('12 senkron testinin hepsi geçti')")
    chrome.click("#history")
    chrome.click("#sheet [data-tab=decisions]")
    chrome.wait("document.querySelector('#sheet [data-panel=decisions]').textContent.includes('Doldurma hangi aydan başlasın?')")
    assert "[object Object]" not in chrome.js("document.body.textContent")


def test_switching_language_in_the_demo_switches_what_the_agents_say(site):
    chrome = site
    assert "Fix flaky sync test" in text(chrome, "#home")
    chrome.js("toggleLang()")
    chrome.wait("document.querySelector('#home').textContent.includes('Kararsız senkron testini düzelt')")
    assert "[object Object]" not in chrome.js("document.body.textContent")


@pytest.mark.parametrize("width", (320, 390))
def test_history_close_button_stays_off_the_tabs(site, width):
    chrome = site
    chrome.resize(width, 780)
    chrome.click("#history")
    chrome.wait("document.querySelector('#sheet .sheet-x') !== null")
    time.sleep(.5)  # the sheet slides up
    overlapping = chrome.js("""(() => { const x = document.querySelector('#sheet .sheet-x').getBoundingClientRect();
        return [...document.querySelectorAll('#sheet [data-tab]')].filter(tab => { const r = tab.getBoundingClientRect();
            return r.left < x.right && r.right > x.left && r.top < x.bottom && r.bottom > x.top; }).map(tab => tab.textContent); })()""")
    assert overlapping == []


@pytest.mark.parametrize("font", ("", "DejaVu Sans, Verdana"))  # the phone's own, and a wide one
@pytest.mark.parametrize("lang", ("en", "tr"))
def test_all_three_limits_fit_the_narrowest_phone(site, lang, font):
    chrome = site
    chrome.resize(320, 700)
    if font:
        chrome.js(f"document.documentElement.style.fontFamily = {json.dumps(font)}")
    chrome.js(f"if (LANG !== {json.dumps(lang)}) toggleLang()")
    chrome.wait("document.querySelectorAll('#limits .lim').length === 3")
    assert chrome.js("(() => { const box = document.querySelector('#limits'); return box.scrollWidth - box.clientWidth; })()") <= 0


def test_limit_figures_stay_clear_of_the_sheet_scrollbar(site):
    chrome = site
    chrome.click("#limits")
    chrome.wait("document.querySelector('#sheet .lt') !== null")
    time.sleep(.5)  # the sheet slides up
    room = chrome.js("""(() => { const box = document.querySelector('#sheet .scroll').getBoundingClientRect();
        return Math.min(...[...document.querySelectorAll('#sheet .lt span:last-child, #sheet .lv.big')].map(el => box.right - el.getBoundingClientRect().right)); })()""")
    assert room >= 10, room


def test_history_tabs_are_a_full_touch_target(site):
    chrome = site
    chrome.click("#history")
    chrome.wait("document.querySelector('#sheet [data-tab]') !== null")
    assert min(chrome.js("[...document.querySelectorAll('#sheet [data-tab]')].map(tab => tab.offsetHeight)")) >= 44


@pytest.mark.parametrize("lang", ("en", "tr"))
def test_demo_conversation_shows_what_this_release_reads(site, lang):
    chrome = site
    chrome.cdp("Page.navigate", url=chrome.js(f"location.origin + '/demo/?speed=8&lang={lang}'"))
    chrome.wait(f"document.documentElement.lang === '{lang}' && document.querySelectorAll('.card').length === 6")
    open_card(chrome, "d5:p1")
    for piece in (".tbl", ".code", ".run", ".act.bad", ".act.denied"):
        assert chrome.js(f"document.querySelector('#messages {piece}') !== null"), piece
    chrome.wait("!document.querySelector('#sMeta').hidden && !document.querySelector('#sRing').hidden")
    meta = text(chrome, "#sMeta")
    assert meta.startswith({"en": "handoff written · ", "tr": "devir yazıldı · "}[lang])
    assert {"en": " · Context 48k · Cache ~", "tr": " · Bağlam 48k · Cache ~"}[lang] in meta
    assert {"en": "You said no", "tr": "Sen reddettin"}[lang] in text(chrome, "#messages .act.denied")
    assert {"en": "Insert only the rows", "tr": "Yalnız eksik satırları"}[lang] in text(chrome, "#messages .act.denied")
    chrome.wait("(() => { const i = document.querySelector('#messages .shot img'); return !!i && i.complete && i.naturalWidth > 0; })()")
    chrome.click("#messages .shot")
    chrome.wait("!document.querySelector('#viewer').hidden && document.querySelector('#viewer img').naturalWidth > 0")
    assert chrome.js("performance.getEntriesByType('resource').filter(r => new URL(r.name).pathname.startsWith('/api')).length") == 0
    assert "[object Object]" not in chrome.js("document.body.textContent")


def test_worst_case_reading_data_stays_inside_a_narrow_phone(site):
    chrome = site
    chrome.resize(320, 700)
    chrome.cdp("Page.navigate", url=chrome.js("location.origin + '/demo/?speed=8&worst'"))
    chrome.wait("document.querySelectorAll('.card').length === 5")
    open_card(chrome, "d1:p1")
    chrome.wait("document.querySelector('#messages .run') !== null && document.querySelector('#messages .tbl') !== null")
    inside = "document.documentElement.scrollWidth <= 320 && [...document.querySelectorAll('#messages .live > *, #session .top > *')].every(e => e.getBoundingClientRect().right <= 320 && e.getBoundingClientRect().left >= 0)"
    assert chrome.js(inside)
    assert chrome.js("document.querySelectorAll('#messages .act.bad, #messages .act.denied').length") == 2
    chrome.wait("document.querySelector('#messages .shot') === null")  # four pictures that cannot load leave no empty boxes
    chrome.click("#messages .run > button")
    chrome.wait("document.querySelectorAll('#messages .run.open .step').length >= 200")
    assert chrome.js(inside)
    chrome.wait("!document.querySelector('#sRing').hidden && !document.querySelector('#sMeta').hidden")
    chrome.click("#sRing")
    chrome.wait("document.querySelectorAll('#sheet .meter').length === 14")
    assert chrome.js("document.documentElement.scrollWidth <= 320 && [...document.querySelectorAll('#sheet .meter')].every(e => e.getBoundingClientRect().right <= 320)")


def test_demo_shows_one_handoff_counting_down_and_one_switched_off(site):
    cards = {pane: (handoff, line) for pane, handoff, line in site.js(
        "[...state.sessions.values()].map(s => [s.pane_id, s.handoff || null, document.querySelector(`.card[data-key=\"${s.pane_id}\"] [data-handoff]`)?.textContent || ''])")}
    assert cards["d5:p1"][0]["phase"] == "written" and cards["d5:p1"][1].startswith("handoff written · clears in ")
    assert cards["d6:p1"][0] == {"on": False, "phase": "armed", "due": None} and cards["d6:p1"][1] == ""
    assert cards["d3:p1"][0] is None and cards["d4:p1"][0] is None  # Codex and agy sessions have no such switch
    site.click('.card[data-key="d5:p1"] [data-handoff-cancel]')
    site.wait("document.querySelector('.card[data-key=\"d5:p1\"] [data-handoff-cancel]') === null")
    assert site.js("state.sessions.get('d5:p1').handoff.phase") == "held"
    site.js("state.idleOpen = true; renderHome()")
    site.click('.card[data-key="d6:p1"] .card-open')
    site.wait("location.hash === '#/s/d6:p1' && !document.querySelector('#sMeta').hidden")
    site.click("#sMeta")
    site.click("#sheet [role=switch]")
    site.wait("state.sessions.get('d6:p1').handoff.on === true && !document.querySelector('#sheet [role=switch]').disabled")
    assert site.js("document.querySelector('#sheet [role=switch]').getAttribute('aria-checked')") == "true"
