"""reader モード（本文の抽出）のテスト。

結合テストではブラウザ（既定はインストール済みの Chrome）を使い、起動できない環境ではスキップする。
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

import pytest

from liteproxy.browser.pool import BrowserPool, Snapshot, Viewport
from liteproxy.config import BrowserConfig, RenderConfig, SessionConfig
from liteproxy.media.image import ImageStore
from liteproxy.render import reader
from liteproxy.security import HostGuard
from tests.sites import ARTICLE_PAGE, INTERACTIVE_PAGE, serve, write_interactive_site

URL = "https://example.com/article.html"
PHOTO = "https://example.com/photo.png"


@pytest.fixture(scope="module")
def article() -> reader.Article:
    document = reader.extract(ARTICLE_PAGE, URL)
    assert document is not None
    make = reader.placeholders({PHOTO: (400, 200)}, {PHOTO: 3456}, 358)
    return reader.build(document, placeholder=make)


def test_only_the_article_is_kept(article):
    html = reader.page(article)
    assert article.title == "本文抽出のテスト記事"
    assert "導入の段落として" in html and "箇条書きの二つ目" in html
    for noise in ("広告枠", "記事一覧", "関連記事その一", "フッターの表記"):
        assert noise not in html, noise
    assert "<h2>最初の見出し</h2>" in html and "<strong>強調した語</strong>" in html
    assert len(html.encode("utf-8")) < 6000  # サイトの CSS を使わないため小さい


def test_links_go_through_the_proxy(article):
    assert f'href="/p?u={quote("https://example.com/next.html", safe="")}"' in article.body


def test_images_become_frames_with_their_size(article):
    assert f'data-lp-src="{PHOTO}"' in article.body
    assert 'data-lp-size="3456"' in article.body and "3.4KB" in article.body
    assert "data:image/svg+xml," in article.body and "width=\"400\" height=\"200\"" in article.body


def test_page_without_article_is_too_short_to_use():
    document = reader.extract(INTERACTIVE_PAGE, "https://example.com/page.html")
    chars = 0 if document is None else reader.build(document, placeholder=lambda u, a: "").chars
    assert chars < RenderConfig().reader_min_chars  # 本文がないため layout モードに切り替わる


# ---------------------------------------------------------------- 結合テスト


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("reader")
    write_interactive_site(root)
    server = serve(root)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def render(url: str, mode: str) -> Snapshot:
    async def main():
        guard = HostGuard(allow_private=True)
        pool = BrowserPool(
            BrowserConfig(channel=os.environ.get("LITEPROXY_TEST_CHANNEL", "chrome"), settle_timeout_ms=2000),
            RenderConfig(),
            guard,
            ImageStore(guard),
            SessionConfig(),
        )
        try:
            await pool.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"ブラウザを起動できません: {e}")
        try:
            return await pool.render(url, Viewport(390, 844, 2, False), None, image_quality="low", mode=mode)
        finally:
            await pool.stop()

    result = asyncio.run(main())
    assert isinstance(result, Snapshot)
    return result


def test_reader_mode_returns_only_the_article(site):
    reader_snap = render(site + "/article.html", "reader")
    layout_snap = render(site + "/article.html", "layout")
    assert reader_snap.mode == "reader" and layout_snap.mode == "layout"
    assert "導入の段落として" in reader_snap.html
    # layout モードには残るナビゲーションやフッターが、reader モードでは落ちる
    assert "記事一覧" in layout_snap.html and "フッターの表記" in layout_snap.html
    assert "記事一覧" not in reader_snap.html and "フッターの表記" not in reader_snap.html
    assert reader_snap.css_bytes == len(reader.READER_CSS)  # サイトの CSS は使わない
    # 本文モードでは操作の中継を使わないため、PC 側のページは保持しない
    assert reader_snap.session_id is None and layout_snap.session_id
    # 画像は枠のまま残り、送信サイズも表示する
    assert "data-lp-src=" in reader_snap.html and "data-lp-size=" in reader_snap.html


def test_reader_mode_falls_back_when_there_is_no_article(site):
    snap = render(site + "/page.html", "reader")
    assert snap.mode == "layout" and "ただの文章" in snap.html
