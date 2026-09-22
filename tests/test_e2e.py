"""liteproxy のサーバーを実際に起動し、スマホ役のブラウザから操作する端から端までのテスト。

PC 側の描画用と、スマホ役の 2 つのブラウザを使う。ブラウザを起動できない環境ではスキップする。
"""

from __future__ import annotations

import os
import socket
import threading
import time
from urllib.parse import quote, urlsplit

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from liteproxy.config import Config
from liteproxy.main import create_app
from tests.sites import serve, write_interactive_site

CHANNEL = os.environ.get("LITEPROXY_TEST_CHANNEL", "chrome") or None


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e")
    write_interactive_site(root)
    server = serve(root)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def proxy():
    config = Config()
    config.network.allow_private = True  # ローカルのテスト用サイトを開くため
    config.browser.channel = CHANNEL or ""
    config.browser.settle_timeout_ms = 2000
    config.log.file = ""
    config.log.stats_file = ""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning", log_config=None)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            pytest.skip("liteproxy を起動できません（ブラウザを起動できない可能性があります）")
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=30)


@pytest.fixture(scope="module")
def phone_browser():
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel=CHANNEL)
        except PlaywrightError as e:
            pytest.skip(f"ブラウザを起動できません: {e}")
        yield browser
        browser.close()


@pytest.fixture
def phone(phone_browser, proxy, site):
    context = phone_browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    # f=1: テストごとに描画し直し、PC 側で新しいページ（セッション）を使う
    page.goto(f"{proxy}/p?u={quote(site + '/page.html', safe='')}&f=1")
    page.wait_for_selector("lp-bar[data-s]", timeout=60000)  # 読み込み中画面から結果のページへ移る
    yield page
    context.close()


def wait_js(page, expression: str, timeout: float = 15.0) -> None:
    """条件が成り立つまで待つ。wait_for_function はページ内で文字列を評価するため、
    liteproxy の CSP（unsafe-eval を許可しない）に拒否される。外から評価して待つ"""
    deadline = time.monotonic() + timeout
    while not page.evaluate(expression):
        if time.monotonic() > deadline:
            raise AssertionError(f"条件が成り立ちません: {expression}")
        time.sleep(0.1)


PANEL = "getComputedStyle(document.getElementById('panel')).display"


def test_toggle_opens_panel_on_phone(phone):
    assert phone.evaluate(PANEL) == "none"
    phone.click("#acc")
    wait_js(phone, f"{PANEL} === 'block'")
    assert phone.get_attribute("#acc", "aria-expanded") == "true"
    phone.click("#acc")
    wait_js(phone, f"{PANEL} === 'none'")


def test_repeated_taps_are_applied_in_order(phone):
    phone.click("#cnt")
    phone.click("#cnt")
    wait_js(phone, "document.getElementById('cnt').textContent === '2'")


def test_added_element_gets_its_css(phone):
    phone.click("#add")
    phone.wait_for_selector("li.new")
    assert phone.evaluate("getComputedStyle(document.querySelector('li.new')).fontWeight") == "700"
    # 差分の後も、別の要素の操作が正しい位置に届く
    phone.click("#acc")
    wait_js(phone, f"{PANEL} === 'block'")


def test_js_navigation_moves_phone(phone, site):
    phone.click("#go")
    phone.wait_for_selector("#next")
    assert quote(site + "/next.html", safe="") in phone.url


def test_plain_text_tap_is_not_forwarded(phone):
    requests: list[str] = []
    phone.on("request", lambda r: requests.append(r.url))
    phone.click("#plain")
    phone.wait_for_timeout(300)
    assert not any(urlsplit(r).path == "/a" for r in requests)
