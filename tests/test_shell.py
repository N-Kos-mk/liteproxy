"""/app のシェル（外枠）の JS を実際のブラウザで動かすテスト。

ブラウザ（既定はインストール済みの Chrome）を起動できない環境ではスキップする。
"""

from __future__ import annotations

import json
import os
from html import escape
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from liteproxy import templates

SHELL = templates.shell("n", icon_src="/icon.png")
ACTIVE = "document.querySelector('#lp-main iframe.on')"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel=os.environ.get("LITEPROXY_TEST_CHANNEL", "chrome") or None)
        except PlaywrightError as e:
            pytest.skip(f"ブラウザを起動できません: {e}")
        yield b
        b.close()


def _result(target: str) -> str:
    """中継したページの代わり。シェルはツールバー（lp-bar）の data-page で元のページを見分ける。"""
    return (
        f"<!DOCTYPE html><html><head><title>T:{escape(target)}</title></head><body>"
        f'<lp-bar hidden data-page="{escape(target)}"></lp-bar>'
        f"<p id='target'>{escape(target)}</p>"
        f'<a id="next" href="/p?u={quote(target + "next/", safe="")}">next</a>'
        f'<form id="form" action="/p"><input name="u" value="{escape(target)}form/"><button id="send">送信</button></form>'
        "<div style='height:3000px'></div></body></html>"
    )


def _loader(target: str) -> str:
    """読み込み中画面の代わり。完了すると /v へ置き換えて移動する。"""
    page = templates.loader_page(target, "n")
    return page + f"<script>location.replace('/v?u={quote(target, safe='')}&j=1')</script></body></html>"


@pytest.fixture
def page(browser):
    requested: list[str] = []

    def handle(route):
        url = urlsplit(route.request.url)
        target = parse_qs(url.query).get("u", [""])[0]
        requested.append(url.path + ("?u=" + target if target else ""))
        if url.path == "/app":
            body = SHELL
        elif url.path == "/p" and target.startswith("https://slow."):
            body = _loader(target)
        elif url.path in ("/p", "/v"):
            body = _result(target)
        else:
            body = "<!DOCTYPE html><html><head><title>home</title></head><body><p>home</p></body></html>"
        route.fulfill(content_type="text/html; charset=utf-8", body=body)

    context = browser.new_context(has_touch=True)
    p = context.new_page()
    p.route("http://lp.test/**", handle)
    p.goto("http://lp.test/app")
    wait_title(p, "home")
    p.requested = requested  # テストから要求された URL を確認する
    yield p
    context.close()


def active_title(page) -> str:
    return page.evaluate(f"{ACTIVE}.contentDocument.title")


def wait_title(page, title: str) -> None:
    page.wait_for_function(
        f"(t) => {{ const f = {ACTIVE}; return f && f.contentDocument && f.contentDocument.title === t }}", arg=title
    )


def open_url(page, url: str) -> None:
    page.fill("#lp-addr-input", url)
    page.press("#lp-addr-input", "Enter")
    wait_title(page, f"T:{url}")


def saved(page) -> dict:
    return json.loads(page.evaluate("localStorage.getItem('lp-tabs')"))


def _selection(page) -> list[int]:
    return page.evaluate("(() => { const i = document.activeElement; return [i.selectionStart, i.selectionEnd] })()")


def test_address_tap_selects_whole_url(page):
    open_url(page, "https://a.example/")
    length = len(page.input_value("#lp-addr-input"))
    page.tap("#lp-addr-input")
    assert page.evaluate("document.activeElement.id") == "lp-addr-input"
    page.wait_for_function("document.activeElement.selectionEnd > 0")
    assert _selection(page) == [0, length]


def test_address_click_selects_whole_url(page):
    open_url(page, "https://a.example/")
    length = len(page.input_value("#lp-addr-input"))
    page.click("#lp-addr-input")
    page.wait_for_function("document.activeElement.selectionEnd > 0")
    assert _selection(page) == [0, length]


def test_back_and_forward_within_tab(page):
    open_url(page, "https://a.example/")
    open_url(page, "https://b.example/")
    assert page.is_disabled("#lp-fwd")
    page.click("#lp-back")
    wait_title(page, "T:https://a.example/")
    page.click("#lp-fwd")
    wait_title(page, "T:https://b.example/")


def test_history_is_separate_per_tab(page):
    open_url(page, "https://a.example/")
    open_url(page, "https://b.example/")
    page.click("#lp-tabs-btn")
    page.click("#lp-tab-new")
    assert page.text_content("#lp-tabs-btn") == "2"
    # 新しいタブではアドレス欄が空のままフォーカスされ、すぐに入力できる
    assert page.evaluate("document.activeElement.id") == "lp-addr-input"
    wait_title(page, "home")
    assert page.input_value("#lp-addr-input") == ""
    open_url(page, "https://c.example/")
    open_url(page, "https://d.example/")

    page.click("#lp-tabs-btn")
    page.click(".lp-tab >> text=T:https://b.example/")
    assert active_title(page) == "T:https://b.example/"
    page.click("#lp-back")
    wait_title(page, "T:https://a.example/")
    # 裏のタブは戻らない
    frames = page.evaluate("[...document.querySelectorAll('#lp-main iframe')].map(f => f.contentDocument.title)")
    assert frames == ["T:https://a.example/", "T:https://d.example/"]


def test_loader_is_not_added_to_history(page):
    open_url(page, "https://a.example/")
    page.fill("#lp-addr-input", "https://slow.example/")
    page.press("#lp-addr-input", "Enter")
    wait_title(page, "T:https://slow.example/")
    assert page.evaluate(f"{ACTIVE}.contentWindow.location.pathname") == "/v"
    page.click("#lp-back")
    wait_title(page, "T:https://a.example/")
    assert [e["page"] for e in saved(page)["tabs"][0]["stack"]] == ["", "https://a.example/", "https://slow.example/"]


def test_reloading_same_page_replaces_history_entry(page):
    open_url(page, "https://a.example/")
    # client.js の読み込み直し（/p?u=...&f=1）や表示モードの切り替えに相当する
    page.evaluate(f"{ACTIVE}.contentWindow.location.href = '/p?u=' + encodeURIComponent('https://a.example/') + '&f=1'")
    page.wait_for_function(f"{ACTIVE}.contentWindow.location.search.endsWith('f=1')")
    page.wait_for_function("JSON.parse(localStorage.getItem('lp-tabs')).tabs[0].stack[1].url.endsWith('f=1')")
    assert len(saved(page)["tabs"][0]["stack"]) == 2


def test_scroll_position_is_restored_on_back(page):
    open_url(page, "https://a.example/")
    page.evaluate(f"{ACTIVE}.contentWindow.scrollTo(0, 500)")
    open_url(page, "https://b.example/")
    page.click("#lp-back")
    wait_title(page, "T:https://a.example/")
    assert page.evaluate(f"{ACTIVE}.contentWindow.scrollY") == 500


def test_tabs_are_restored_and_background_tab_loads_on_demand(page):
    open_url(page, "https://a.example/")
    page.click("#lp-tabs-btn")
    page.click("#lp-tab-new")
    wait_title(page, "home")
    open_url(page, "https://b.example/")

    page.requested.clear()
    page.reload()
    wait_title(page, "T:https://b.example/")
    assert page.text_content("#lp-tabs-btn") == "2"
    # 開き直したときは表示中のタブだけを読み込む
    assert not any("a.example" in r for r in page.requested)
    page.click("#lp-tabs-btn")
    page.click(".lp-tab >> text=T:https://a.example/")
    wait_title(page, "T:https://a.example/")
    assert any("a.example" in r for r in page.requested)
    # 復元したタブでも、それまでの履歴で戻れる
    page.click("#lp-back")
    wait_title(page, "home")


def test_closing_active_tab_switches_to_neighbor(page):
    open_url(page, "https://a.example/")
    page.click("#lp-tabs-btn")
    page.click("#lp-tab-new")
    wait_title(page, "home")
    open_url(page, "https://b.example/")
    page.click("#lp-tabs-btn")
    page.click("#lp-tab-list li.on .lp-x")
    assert page.text_content("#lp-tabs-btn") == "1"
    assert active_title(page) == "T:https://a.example/"
    # 最後のタブを閉じたときは新しいタブを開く
    page.click("#lp-tab-list li.on .lp-x")
    wait_title(page, "home")
    assert page.text_content("#lp-tabs-btn") == "1"


def history_length(page) -> int:
    return page.evaluate("history.length")


def test_address_shows_original_url(page):
    # liteproxy の URL（/p?u=...・/v?u=...&j=...）ではなく、元のページの URL を表示する
    assert page.input_value("#lp-addr-input") == ""  # ホーム
    open_url(page, "https://a.example/")
    assert page.input_value("#lp-addr-input") == "https://a.example/"
    page.fill("#lp-addr-input", "https://slow.example/")
    page.press("#lp-addr-input", "Enter")
    wait_title(page, "T:https://slow.example/")
    assert page.input_value("#lp-addr-input") == "https://slow.example/"


def test_navigation_inside_tab_does_not_add_browser_history(page):
    # iframe の履歴はタブの間で共有されるため、iframe 内の移動では履歴を積まない
    open_url(page, "https://a.example/")
    before = history_length(page)
    page.frame_locator("#lp-main iframe.on").locator("#next").click()
    wait_title(page, "T:https://a.example/next/")
    page.frame_locator("#lp-main iframe.on").locator("#send").click()
    wait_title(page, "T:https://a.example/next/form/")
    open_url(page, "https://b.example/")
    assert history_length(page) == before
    page.click("#lp-back")
    wait_title(page, "T:https://a.example/next/form/")
    page.click("#lp-back")
    wait_title(page, "T:https://a.example/next/")


def test_device_back_goes_back_in_active_tab(page):
    # Android の戻るジェスチャーは CloseWatcher で受け取る（デスクトップの Chrome では Esc が同じ扱い）
    open_url(page, "https://a.example/")
    open_url(page, "https://b.example/")
    page.frame_locator("#lp-main iframe.on").locator("#next").click()
    wait_title(page, "T:https://b.example/next/")
    before = history_length(page)
    page.keyboard.press("Escape")  # フォーカスは iframe の中
    wait_title(page, "T:https://b.example/")
    page.focus("#lp-back")
    page.keyboard.press("Escape")  # フォーカスはシェル
    wait_title(page, "T:https://a.example/")
    assert history_length(page) == before


def test_device_back_is_left_to_browser_when_tab_cannot_go_back(page):
    # タブで戻れないときは受け取らず、「戻る」でアプリを閉じられるようにする
    page.focus("#lp-back")
    assert page.evaluate("window.CloseWatcher !== undefined")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert active_title(page) == "home"
    open_url(page, "https://a.example/")
    page.click("#lp-back")
    wait_title(page, "home")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert active_title(page) == "home"
