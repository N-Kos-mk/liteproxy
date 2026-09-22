"""スマホ側の client.js を実際のブラウザで動かすテスト。

ブラウザ（既定はインストール済みの Chrome）を起動できない環境ではスキップする。
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from liteproxy.templates import BAR_CSS

CLIENT_JS = (Path(__file__).resolve().parent.parent / "liteproxy" / "static" / "client.js").read_text(encoding="utf-8")
PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='400' height='200'%3E%3C/svg%3E"
PAGE = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{BAR_CSS}</style>
<style>#c {{ width: 40px; }} #tall {{ height: 3000px; }}</style></head><body>
<lp-bar data-page="https://e.com/article" data-q="mid" data-dq="mid"><button type="button" id="lp-img">画像</button></lp-bar>
<img id="a" src="{PLACEHOLDER}" data-lp-src="https://e.com/a.jpg" data-lp-size="12345" alt="写真A">
<a id="link" href="/next"><img id="b" src="{PLACEHOLDER}" data-lp-src="https://e.com/b.jpg" alt=""></a>
<img id="c" src="{PLACEHOLDER}" data-lp-src="https://e.com/c.jpg" alt="">
<div id="tall"></div>
<script src="/static/client.js"></script>
</body></html>"""


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 160), (30, 120, 200)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel=os.environ.get("LITEPROXY_TEST_CHANNEL", "chrome") or None)
        except PlaywrightError as e:
            pytest.skip(f"ブラウザを起動できません: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    png = _png()
    requested: list[dict] = []

    def handle(route):
        url = urlsplit(route.request.url)
        if url.path == "/page":
            route.fulfill(content_type="text/html; charset=utf-8", body=PAGE)
        elif url.path == "/static/client.js":
            route.fulfill(content_type="text/javascript", body=CLIENT_JS)
        elif url.path == "/i":
            requested.append({k: v[0] for k, v in parse_qs(url.query).items()})
            route.fulfill(content_type="image/png", body=png)
        elif url.path == "/next":
            route.fulfill(content_type="text/html", body="<p id='next'>next</p>")
        else:
            route.fulfill(status=404, body="")

    context = browser.new_context()
    p = context.new_page()
    p.route("http://lp.test/**", handle)
    p.goto("http://lp.test/page")
    p.requested = requested  # テストから /i への要求を確認する
    yield p
    context.close()


def wait_loaded(page, selector: str) -> None:
    page.wait_for_function(f"document.querySelector('{selector}').dataset.lpState === 'done'")


def test_tap_loads_image_with_default_quality(page):
    page.click("#a")
    wait_loaded(page, "#a")
    assert page.requested == [{"u": "https://e.com/a.jpg", "q": "mid", "r": "https://e.com/article"}]
    assert page.get_attribute("#a", "src").startswith("/i?")
    # 低画質の画像でもレイアウトが変わらないよう、枠の寸法を属性として固定する
    assert page.get_attribute("#a", "width") == "400" and page.get_attribute("#a", "height") == "200"


def test_image_in_link_loads_first_then_follows_link(page):
    page.click("#b")
    wait_loaded(page, "#b")
    assert urlsplit(page.url).path == "/page"  # 1 回目は読み込むだけで移動しない
    page.click("#b")
    page.wait_for_selector("#next")
    assert urlsplit(page.url).path == "/next"


def test_load_all_from_toolbar(page):
    page.click("#lp-img")
    sheet = page.locator("lp-sheet")
    assert "未読み込み 3 枚" in sheet.inner_text()
    assert "約12KB以上" in sheet.inner_text()  # サイズが分かる画像の合計（分からないものがあるため「以上」）
    page.get_by_text("すべて読み込む").click()
    wait_loaded(page, "#a")
    wait_loaded(page, "#b")
    assert sorted(r["u"] for r in page.requested) == ["https://e.com/a.jpg", "https://e.com/b.jpg", "https://e.com/c.jpg"]


def test_default_quality_is_saved_in_cookie(page):
    page.click("#lp-img")
    page.locator("lp-sheet button", has_text="低").click()
    assert "lp_q=low" in page.evaluate("document.cookie")
    page.click("#a")
    wait_loaded(page, "#a")
    assert page.requested[-1]["q"] == "low"


def test_long_press_menu_selects_quality(page):
    page.click("#a", button="right")  # PC の右クリック（Android の長押しと同じ contextmenu）
    page.locator("lp-sheet button", has_text="原本").click()
    wait_loaded(page, "#a")
    assert page.requested == [{"u": "https://e.com/a.jpg", "q": "orig", "r": "https://e.com/article"}]


def test_size_is_preserved_when_layout_depends_on_the_image(page):
    """寸法の指定がない画像は、低画質で固有サイズが小さくなっても表示サイズを保つ。"""
    before = page.evaluate("document.getElementById('a').getBoundingClientRect().height")
    page.click("#a")
    wait_loaded(page, "#a")
    assert page.evaluate("document.getElementById('a').naturalHeight") == 160  # 受け取った画像は小さい
    assert page.evaluate("document.getElementById('a').getBoundingClientRect().height") == before == 200


def test_site_specified_size_is_not_overwritten(page):
    """幅だけを指定し高さを縦横比から決めているサイトで、高さを固定して崩さない。"""
    assert page.evaluate("getComputedStyle(document.getElementById('c')).height") == "20px"
    page.click("#c")
    wait_loaded(page, "#c")
    assert page.evaluate("getComputedStyle(document.getElementById('c')).height") == "20px"
    assert page.get_attribute("#c", "height") is None
    assert page.get_attribute("#c", "style") is None


def test_toolbar_stays_at_the_top_while_scrolling(page):
    page.evaluate("scrollTo(0, 1200)")
    page.wait_for_timeout(100)
    assert page.evaluate("scrollY") > 1000
    assert page.evaluate("Math.round(document.querySelector('lp-bar').getBoundingClientRect().top)") == 0
