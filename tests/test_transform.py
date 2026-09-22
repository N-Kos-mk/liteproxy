"""ローカルの HTTP サーバーで用意したページを実際に描画・変換する結合テスト。

ブラウザ（既定はインストール済みの Chrome）を起動できない環境ではスキップする。
LITEPROXY_TEST_CHANNEL="" とすると Playwright 同梱の Chromium を使う。
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import os
import random
import re
import threading
from urllib.parse import quote

import pytest
from PIL import Image

from liteproxy.browser.pool import BrowserPool, Snapshot, Viewport
from liteproxy.config import BrowserConfig, RenderConfig
from liteproxy.media.image import ImageStore
from liteproxy.security import HostGuard

BIG_PATH = "M0 0" + " L1 1" * 800
SITE_ROOT: list[str] = []

PAGE = """<!doctype html>
<html lang="ja" class="js-root">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta property="og:title" content="og">
<title>テストページ</title>
<link rel="stylesheet" href="/same.css">
<link rel="stylesheet" href="http://127.0.0.1:{PORT2}/cross.css">
<style>
  .used { color: red; }
  .unused-rule { color: blue; }
  .card:hover { color: green; }
  @media print { .used { color: black; } }
  @media (min-width: 2000px) { .used { color: purple; } }
  @font-face { font-family: X; src: url(/x.woff2); }
  .bg { background: #fff url(/bg.png) no-repeat; }
  .icon { background: url("data:image/svg+xml,%3Csvg%3E%3C/svg%3E"); }
  x-widget:not(:defined) { display: none; }
  @keyframes spin { to { transform: rotate(1turn); } }
  @keyframes unusedanim { to { opacity: 0; } }
  .spinner { animation: spin 1s infinite; }
  [data-state="open"] { outline: 1px solid; }
</style>
<script>
  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('dyn').textContent = 'JSで生成';
    document.getElementById('q').value = 'JSで変更';
  });
</script>
</head>
<body>
<p id="dyn"></p>
<p class="used jsonly-hook">テキスト</p>
<div class="card bg spinner icon" data-state="open" data-tracking="abc" onclick="alert(1)">カード</div>
<img id="i1" src="/img.svg" alt="説明" width="200" height="100">
<img id="i4" src="/photo.jpg" alt="写真">
<img id="i2" src="/img.svg" srcset="/img.svg 1x, /img2.svg 2x" alt="">
<picture><source srcset="/img2.svg" media="(min-width: 1px)"><img id="i3" src="/img.svg" alt=""></picture>
<a id="l1" href="/next.html?a=1">次へ</a>
<a id="l2" href="#sec">節へ</a>
<a id="l3" href="mailto:a@example.com">mail</a>
<form id="f1" action="/search" method="get">
  <input id="q" name="q" value="init">
  <input type="password" name="pw" value="secret">
</form>
<iframe id="if1" src="/frame.html" width="320" height="180"></iframe>
<video id="v1" src="/movie.mp4" width="320" height="180" autoplay controls></video>
<x-widget>ウィジェット</x-widget>
<svg id="big" width="100" height="100"><path d="{BIG_PATH}"/></svg>
<svg id="small" width="16" height="16"><use href="#ic"/></svg>
<svg style="display:none"><symbol id="ic"><path d="M0 0h16v16H0z"/></symbol><symbol id="unused-ic"><path d="M1 1h2v2H1z"/></symbol></svg>
<div id="onetrust-consent-sdk">同意バナー</div>
<p class="same-used cross-used">外部CSS</p>
<h2 id="sec">節</h2>
<!-- comment -->
</body>
</html>
"""

SJIS_PAGE = """<!doctype html>
<html><head><meta charset="Shift_JIS"><title>SJIS</title></head>
<body><p>日本語テキスト</p><form action="/s"><input name="q"></form></body></html>
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    # Windows ではレジストリ次第で MIME タイプがずれるため明示する
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html",
        ".css": "text/css",
        ".svg": "image/svg+xml",
    }

    def log_message(self, format, *args):  # noqa: A002
        pass


def _serve(directory: str) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=directory))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    SITE_ROOT[:] = [str(root)]
    main = _serve(str(root))
    cross = _serve(str(root))  # ポート違い = 別オリジン
    port, port2 = main.server_address[1], cross.server_address[1]

    page = PAGE.replace("{PORT2}", str(port2)).replace("{BIG_PATH}", BIG_PATH)
    (root / "page.html").write_text(page, encoding="utf-8")
    (root / "sjis.html").write_bytes(SJIS_PAGE.encode("cp932"))
    (root / "same.css").write_text(".same-used{margin:0}.same-unused{margin:1px}", encoding="utf-8")
    (root / "cross.css").write_text(".cross-used{padding:0}.cross-unused{padding:1px}", encoding="utf-8")
    (root / "img.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='400' height='200'></svg>", encoding="utf-8"
    )
    (root / "img2.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='800' height='400'></svg>", encoding="utf-8"
    )
    (root / "frame.html").write_text("<p>frame</p>", encoding="utf-8")
    rnd = random.Random(0)
    noise = bytes(rnd.getrandbits(8) for _ in range(800 * 400 * 3))
    Image.frombytes("RGB", (800, 400), noise).save(root / "photo.jpg", "JPEG", quality=90)
    yield port
    main.shutdown()
    cross.shutdown()


def _render(url: str) -> Snapshot:
    async def run():
        guard = HostGuard(allow_private=True)
        pool = BrowserPool(
            BrowserConfig(channel=os.environ.get("LITEPROXY_TEST_CHANNEL", "chrome"), settle_timeout_ms=2000),
            RenderConfig(),
            guard,
            ImageStore(guard),
        )
        try:
            await pool.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"ブラウザを起動できません: {e}")
        try:
            return await pool.render(url, Viewport(390, 844, 2, False), None, image_quality="low")
        finally:
            await pool.stop()

    result = asyncio.run(run())
    assert isinstance(result, Snapshot)
    return result


@pytest.fixture(scope="module")
def snap(site) -> Snapshot:
    return _render(f"http://127.0.0.1:{site}/page.html")


@pytest.fixture(scope="module")
def css(snap) -> str:
    m = re.search(r"<style>(.*?)</style>", snap.html, re.S)
    assert m, "抽出した CSS が head に入っていない"
    return m.group(1)


def _tag(html: str, tag: str, id_: str) -> str:
    m = re.search(rf'<{tag}\b[^>]*\bid="{id_}"[^>]*>', html)
    assert m, f"<{tag} id={id_}> が見つからない"
    return m.group(0)


def test_no_external_references(snap):
    # スマホ側で外部への通信が発生する参照が残っていないこと
    assert not re.findall(r'(?<![-\w])(?:src|href|poster|background|action)="https?://', snap.html)
    assert "srcset" not in snap.html
    assert not re.search(r"url\((?![\"']?(?:data:|#))", snap.html)
    assert "<script" not in snap.html
    assert "<link" not in snap.html
    assert "<source" not in snap.html


def test_document_basics(snap):
    assert snap.html.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in snap.html
    assert "og:title" not in snap.html
    assert "<!-- comment" not in snap.html
    assert "<!--lp-bar-->" in snap.html
    assert snap.title == "テストページ"
    assert snap.pc_bytes > 0


def test_js_state_is_captured(snap):
    assert "JSで生成" in snap.html
    assert 'value="JSで変更"' in _tag(snap.html, "input", "q")


def test_image_placeholder_keeps_intrinsic_size(snap, site):
    i1 = _tag(snap.html, "img", "i1")
    assert "data:image/svg+xml," in i1
    assert "width='400' height='200'" in i1  # 画像ファイル本来の固有サイズ
    assert "説明" in i1
    assert f'data-lp-src="http://127.0.0.1:{site}/img.svg"' in i1
    assert 'width="200" height="100"' in i1  # 元の属性は残す
    assert "data:image/svg+xml," in _tag(snap.html, "img", "i3")


def test_css_is_pruned(css):
    assert ".used" in css
    assert ".unused-rule" not in css
    assert ".card:hover" in css  # 状態依存のルールは、基になる要素があれば残す
    assert "@font-face" not in css
    assert "2000px" not in css and "print" not in css
    assert "x-widget:not(:is(*))" in css
    assert "spin" in css and "unusedanim" not in css
    assert ".same-used" in css and ".same-unused" not in css
    assert ".cross-used" in css and ".cross-unused" not in css  # クロスオリジン CSS
    assert "data:image/svg+xml" in css  # 小さな data: URI は残す


def test_attributes_are_pruned(snap):
    assert 'class="used"' in snap.html  # CSS で使われない jsonly-hook は削る
    assert 'data-state="open"' in snap.html  # CSS から参照される data-* は残す
    assert "data-tracking" not in snap.html
    assert "onclick" not in snap.html


def test_links_and_forms(snap, site):
    l1 = _tag(snap.html, "a", "l1")
    assert 'href="/p?u=' + quote(f"http://127.0.0.1:{site}/next.html?a=1", safe="") + '"' in l1
    assert 'href="#sec"' in _tag(snap.html, "a", "l2")
    assert 'href="mailto:a@example.com"' in _tag(snap.html, "a", "l3")
    f1 = _tag(snap.html, "form", "f1")
    assert 'action="/f"' in f1 and 'method="get"' in f1
    assert f'name="__lp_action" value="http://127.0.0.1:{site}/search"' in snap.html
    assert "secret" not in snap.html


def test_embeds_are_replaced(snap):
    if1 = _tag(snap.html, "iframe", "if1")
    assert "srcdoc=" in if1 and 'width="320"' in if1
    v1 = _tag(snap.html, "video", "v1")
    assert "poster=" in v1 and "autoplay" not in v1 and "controls" not in v1
    assert "L1 1 L1 1" not in snap.html  # 大きなインライン SVG は中身を捨てる
    assert 'id="ic"' in snap.html and 'id="unused-ic"' not in snap.html
    assert "同意バナー" not in snap.html


def test_shift_jis_page(site):
    snap = _render(f"http://127.0.0.1:{site}/sjis.html")
    assert "日本語テキスト" in snap.html
    assert '<meta charset="utf-8">' in snap.html
    assert 'name="__lp_charset" value="Shift_JIS"' in snap.html


def test_image_sizes_are_precomputed(snap, site):
    photo = _tag(snap.html, "img", "i4")
    size = int(re.search(r'data-lp-size="(\d+)"', photo).group(1))
    original = os.path.getsize(os.path.join(SITE_ROOT[0], "photo.jpg"))
    assert 0 < size < original  # 低画質の WebP に変換した後のサイズ
    assert "KB" in photo  # 枠にサイズを表示する
    # 変換できない SVG は元のサイズをそのまま表示する
    svg = _tag(snap.html, "img", "i1")
    svg_size = os.path.getsize(os.path.join(SITE_ROOT[0], "img.svg"))
    assert f'data-lp-size="{svg_size}"' in svg
    assert snap.image_quality == "low" and snap.image_count >= 2
