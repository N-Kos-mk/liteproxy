"""/app のシェル（外枠）の JS を実際のブラウザで動かすテスト。

ブラウザ（既定はインストール済みの Chrome）を起動できない環境ではスキップする。
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from liteproxy import templates

SHELL = templates.shell("n", icon_src="/icon.png")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel=os.environ.get("LITEPROXY_TEST_CHANNEL", "chrome") or None)
        except PlaywrightError as e:
            pytest.skip(f"ブラウザを起動できません: {e}")
        yield b
        b.close()


def _content(path: str) -> str:
    return f"<!DOCTYPE html><html><head><title>{path}</title></head><body><p id='path'>{path}</p></body></html>"


@pytest.fixture
def page(browser):
    def handle(route):
        path = urlsplit(route.request.url).path
        if path == "/app":
            route.fulfill(content_type="text/html; charset=utf-8", body=SHELL)
        else:
            route.fulfill(content_type="text/html; charset=utf-8", body=_content(path))

    context = browser.new_context(has_touch=True)
    p = context.new_page()
    p.route("http://lp.test/**", handle)
    p.goto("http://lp.test/app")
    p.wait_for_function("document.getElementById('lp-addr-input').value.endsWith('/')")
    yield p
    context.close()


def _selection(page) -> list[int]:
    return page.evaluate("(() => { const i = document.activeElement; return [i.selectionStart, i.selectionEnd] })()")


def test_address_tap_selects_whole_url(page):
    length = len(page.input_value("#lp-addr-input"))
    page.tap("#lp-addr-input")
    assert page.evaluate("document.activeElement.id") == "lp-addr-input"
    page.wait_for_function("document.activeElement.selectionEnd > 0")
    assert _selection(page) == [0, length]


def test_address_click_selects_whole_url(page):
    length = len(page.input_value("#lp-addr-input"))
    page.click("#lp-addr-input")
    page.wait_for_function("document.activeElement.selectionEnd > 0")
    assert _selection(page) == [0, length]
