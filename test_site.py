import hashlib
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_web import open_chrome
from tools import build_site

PAGES = ("index.html", "tr/index.html")


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    return build_site.build(tmp_path_factory.mktemp("site"))


def host_headers(out):
    """The headers the static host adds to every response, read from the same _headers file it reads."""
    lines = (out / "_headers").read_text().splitlines()
    return [tuple(part.strip() for part in line.split(":", 1)) for line in lines if line.startswith("  ")]


@pytest.fixture
def served(out, tmp_path):
    seen = []

    class Host(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def end_headers(self):
            seen.append(self.path)
            for name, value in host_headers(out):
                self.send_header(name, value.replace("; upgrade-insecure-requests", ""))  # the test host is plain http
            super().end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Host, directory=str(out)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with open_chrome(tmp_path) as chrome:
            yield chrome, f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()


def test_every_local_link_and_picture_exists_in_the_build(out):
    for page in PAGES:
        html = (out / page).read_text()
        refs = re.findall(r'(?:href|src|data-demo)="([^"#]+)', html)
        local = [r for r in refs if not r.startswith(("http", "mailto:"))]
        assert len(local) > 10
        for ref in local:
            ref = ref.split('?')[0]
            target = (out / page).parent / ref
            assert (target / "index.html").is_file() if ref.endswith("/") else target.is_file(), f"{page}: {ref}"


def test_pages_ask_nothing_of_anyone_else(out):
    for page in PAGES + ("site.css", "site.js"):
        text = (out / page).read_text()
        loads = re.findall(r'(?:src|srcset)="(https?:[^"]+)|url\((https?:[^)]+)|<link rel="(?:stylesheet|preload|preconnect)"[^>]*href="(https?:[^"]+)', text)
        assert not loads, f"{page} loads {loads}"
    assert 'style="' not in (out / "index.html").read_text()  # the host's policy has no need for inline styles from this page


def test_both_languages_say_the_same_things(out):
    en, tr = ((out / page).read_text() for page in PAGES)
    for tag in ("<h2", "<h3", "<li", "<img", "<a "):
        assert en.count(tag) == tr.count(tag), tag
    assert 'lang="en"' in en and 'lang="tr"' in tr


def test_phone_page_fits_and_keeps_the_recording(served):
    chrome, url, seen = served
    for path in ("/", "/tr/"):
        chrome.resize(390, 780)
        chrome.cdp("Page.navigate", url=url + path)
        chrome.wait("document.readyState === 'complete' && document.querySelector('.phone img').complete")
        assert chrome.js("document.documentElement.scrollWidth") <= 390, path
        assert chrome.js("document.querySelector('.phone iframe') === null")
        assert chrome.js("[...document.querySelectorAll('.btn')].every(b => b.getBoundingClientRect().height >= 44)")
        assert chrome.js("document.querySelector('.phone img').naturalWidth") > 0


def test_wide_page_runs_the_demo_under_the_hosts_own_policy(served):
    chrome, url, seen = served
    chrome.resize(1280, 900)
    chrome.cdp("Page.navigate", url=url + "/")
    chrome.wait("document.querySelector('.phone.live iframe') !== null")
    chrome.wait("document.querySelector('.phone iframe').contentDocument.querySelectorAll('.card').length === 6")
    assert chrome.js("document.documentElement.scrollWidth") <= 1280
    assert not [path for path in seen if "/api" in path]


def test_phone_frame_keeps_the_demo_at_phone_width_and_inside_the_glass(served):
    chrome, url, seen = served
    chrome.resize(1280, 900)
    chrome.cdp("Page.navigate", url=url + "/tr/")
    chrome.wait("document.querySelector('.phone.live iframe') !== null")
    chrome.wait("document.querySelector('.phone iframe').contentDocument.querySelectorAll('.card').length === 6")
    # The demo lays out as a 390 px phone and is scaled as a whole; its edges stay inside the glass with the bezel around it.
    assert chrome.js("document.querySelector('.phone iframe').contentWindow.innerWidth") == 390
    assert chrome.js("""(() => { const g = document.querySelector('.phone .glass').getBoundingClientRect(), f = document.querySelector('.phone iframe').getBoundingClientRect(), p = document.querySelector('.phone').getBoundingClientRect();
        return f.left >= g.left - .5 && f.right <= g.right + .5 && f.bottom <= g.bottom + .5 && g.left - p.left >= 10 && p.right - g.right >= 10; })()""")
    assert chrome.js("document.querySelector('.phone iframe').contentDocument.documentElement.lang || document.querySelector('.phone iframe').contentWindow.LANG") in ("tr", "TR")


def test_theme_button_on_the_page_also_turns_the_demo(served):
    chrome, url, seen = served
    chrome.resize(1280, 900)
    chrome.cdp("Page.navigate", url=url + "/")
    chrome.wait("document.querySelector('.phone.live iframe') !== null")
    before = chrome.js("document.documentElement.dataset.theme")
    chrome.click(".theme")
    chrome.wait(f"document.documentElement.dataset.theme !== '{before}'")
    chrome.wait("document.querySelector('.phone iframe').contentDocument.documentElement.dataset.theme === document.documentElement.dataset.theme")
    chrome.js("localStorage.removeItem('theme')")


def test_the_mark_note_stays_small(served):
    chrome, url, seen = served
    for path in ("/", "/tr/"):
        for width, most in ((1280, 240), (390, 420)):
            chrome.resize(width, 900)
            chrome.cdp("Page.navigate", url=url + path)
            chrome.wait("document.readyState === 'complete' && document.querySelector('#mark') !== null")
            mark, run = (chrome.js(f"document.querySelector('{s}').getBoundingClientRect().height") for s in ("#mark", "#run"))
            assert mark <= most and mark < run, (path, width, mark, run)
            assert chrome.js("document.querySelector('#mark .sign').getBoundingClientRect().width") <= 32, path
            assert chrome.js("document.documentElement.scrollWidth") <= width, path


def test_both_pages_say_what_the_mark_means(out):
    for page in PAGES:
        html = (out / page).read_text()
        part = html.split('<section id="mark">')[1].split("</section>")[0]
        # a footnote for the curious: the last thing before the footer, with no headings of its own below the title
        assert html.index('<section id="mark">') > html.index('<section id="run">'), page
        assert html.split('<section id="mark">')[1].split("</section>")[1].strip().startswith("</main>"), page
        assert "<h3" not in part and 'class="intro"' not in part, page
        assert part.count("<li") == 3 and part.count("<svg") == 4, page
        # pressed wax, the ring the press leaves, the U: nothing about a corner any more
        assert part.count('href="#wax"') == 4 and "corner" not in part and "köşe" not in part, page


def test_the_host_cannot_rewrite_the_install_command(out):
    # Cloudflare's e-mail obfuscation swaps anything shaped like an address for a script-decoded link, even inside <pre>.
    for page in PAGES:
        html = (out / page).read_text()
        assert "<!--email_off--><pre>" in html and "</pre><!--/email_off-->" in html, page
        assert html.count("@") == html.split("<!--email_off-->")[1].split("<!--/email_off-->")[0].count("@"), page


def test_small_phones_do_not_scroll_sideways(served):
    chrome, url, seen = served
    for path in ("/", "/tr/"):
        for width in (375, 360, 320):
            chrome.resize(width, 700)
            chrome.cdp("Page.navigate", url=url + path)
            chrome.wait("document.readyState === 'complete' && document.querySelector('.route .end') !== null")
            # measured against the phone, not innerWidth: a phone browser widens innerWidth to fit whatever sticks out
            assert chrome.js("document.documentElement.scrollWidth") <= width, (path, width)
            # the road line under the heading ends inside the page gutter (layout box: the opening animation scales it)
            assert chrome.js("(() => { const e = document.querySelector('.route .end'); return e.offsetLeft + e.offsetWidth <= document.documentElement.clientWidth - 16; })()"), (path, width)
            # nor while the page opens: the last stop waits two seconds enlarged and unseen, and that must not widen the page
            chrome.js("document.querySelector('.route .end').getAnimations().forEach(a => { a.pause(); a.currentTime = 0; })")
            assert chrome.js("document.documentElement.scrollWidth") <= width, (path, width, "opening")


def test_no_marker_keeps_a_sharp_corner(out):
    assert not re.search(r"border-radius:50% \d+px 50% 50%", (out / "site.css").read_text())


def test_the_four_stops_line_up_on_a_wide_screen(served):
    chrome, url, seen = served
    for path in ("/", "/tr/"):
        chrome.resize(1280, 900)
        chrome.cdp("Page.navigate", url=url + path)
        chrome.wait("document.readyState === 'complete' && document.querySelectorAll('.road figure').length === 4")
        tops = chrome.js("[...document.querySelectorAll('.road figure')].map(f => Math.round(f.getBoundingClientRect().top + scrollY))")
        assert len(set(tops)) == 1, (path, tops)


def test_a_wrong_address_gets_a_page_that_says_so(out):
    # Without 404.html the host answers every unknown path with the landing page and a 200.
    html = (out / "404.html").read_text()
    assert 'href="/"' in html and 'href="/site.css?v=' in html and 'name="robots" content="noindex"' in html
    assert not re.findall(r'(?:src|href)="(?!/|https://github\.com/)', html)  # served at any depth, so every path is absolute


def test_a_new_build_reaches_returning_visitors_at_once(out):
    # The host's default lets a browser keep site.css and the demo's scripts for four hours, so a fresh page met a stale stylesheet.
    assert ("Cache-Control", "public, max-age=0, must-revalidate") in host_headers(out)


def test_every_stylesheet_and_script_address_changes_with_its_content(out):
    # A host or browser that keeps site.css for hours still fetches a changed one: its address is new.
    for page in PAGES + ("404.html", "demo/index.html"):
        html = (out / page).read_text()
        refs = re.findall(r'(?:href|src)="([^"]+\.(?:css|js)[^"]*)"', html)
        assert len(refs) >= 2, page
        for ref in refs:
            path, _, stamp = ref.partition("?v=")
            target = out / path.lstrip("/") if path.startswith("/") else (out / page).parent / path
            assert stamp == hashlib.sha256(target.read_bytes()).hexdigest()[:8], f"{page}: {ref}"
