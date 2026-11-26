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
        assert chrome.js("document.documentElement.scrollWidth <= innerWidth"), path
        assert chrome.js("document.querySelector('.phone iframe') === null")
        assert chrome.js("[...document.querySelectorAll('.btn')].every(b => b.getBoundingClientRect().height >= 44)")
        assert chrome.js("document.querySelector('.phone img').naturalWidth") > 0


def test_wide_page_runs_the_demo_under_the_hosts_own_policy(served):
    chrome, url, seen = served
    chrome.resize(1280, 900)
    chrome.cdp("Page.navigate", url=url + "/")
    chrome.wait("document.querySelector('.phone.live iframe') !== null")
    chrome.wait("document.querySelector('.phone iframe').contentDocument.querySelectorAll('.card').length === 6")
    assert chrome.js("document.documentElement.scrollWidth <= innerWidth")
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


def test_both_pages_say_what_the_mark_means(out):
    for page in PAGES:
        html = (out / page).read_text()
        part = html.split('<section id="mark">')[1].split("</section>")[0]
        assert part.count("<li") == 3 and part.count("<svg") == 4, page
        # pressed wax, the ring the press leaves, the U: nothing about a corner any more
        assert part.count('href="#wax"') == 4 and "corner" not in part and "köşe" not in part, page
