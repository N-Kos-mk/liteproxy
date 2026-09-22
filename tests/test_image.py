"""画像の変換・保持・取得のテスト。"""

from __future__ import annotations

import asyncio
import functools
import http.server
import io
import random
import threading

import pytest
from PIL import Image

from liteproxy.media.image import ImageData, ImageStore, convert
from liteproxy.security import HostGuard


def jpeg(width: int, height: int) -> ImageData:
    rnd = random.Random(0)
    img = Image.frombytes("RGB", (width, height), bytes(rnd.getrandbits(8) for _ in range(width * height * 3)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return ImageData(out.getvalue(), "image/jpeg")


def size_of(data: ImageData) -> tuple[int, int]:
    with Image.open(io.BytesIO(data.body)) as img:
        return img.size


@pytest.fixture(scope="module")
def photo() -> ImageData:
    return jpeg(1600, 800)


@pytest.mark.parametrize(("quality", "expected"), [("low", (320, 160)), ("mid", (640, 320)), ("high", (1170, 585))])
def test_convert_resizes_to_quality(photo, quality, expected):
    out = convert(photo, quality, screen_px=1170)
    assert out.content_type == "image/webp"
    assert size_of(out) == expected
    assert len(out.body) < len(photo.body)


def test_convert_never_upscales():
    small = jpeg(200, 100)
    out = convert(small, "high", screen_px=1170)
    assert size_of(out) == (200, 100)


def test_orig_is_returned_as_is(photo):
    assert convert(photo, "orig", screen_px=1170) is photo


def test_unsupported_or_larger_result_returns_original():
    svg = ImageData(b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>", "image/svg+xml")
    assert convert(svg, "low", screen_px=1170) is svg
    broken = ImageData(b"\xff\xd8not a jpeg", "image/jpeg")
    assert convert(broken, "low", screen_px=1170) is broken


def test_store_uses_captured_original_without_fetching(photo):
    calls = []

    async def fetcher(url, referer, ua):
        calls.append(url)
        return None

    async def run():
        store = ImageStore(HostGuard(allow_private=True), fetcher=fetcher)
        store.add_original("https://e.com/a.jpg", photo)
        sizes = await store.prepare(["https://e.com/a.jpg", "https://e.com/missing.jpg"], "low", 1170)
        assert list(sizes) == ["https://e.com/a.jpg"]  # 取得していない画像は後回し（タップ時に取得）
        low = await store.variant("https://e.com/a.jpg", "low", 1170, referer=None, user_agent=None)
        assert len(low.body) == sizes["https://e.com/a.jpg"]
        assert await store.variant("https://e.com/missing.jpg", "low", 1170, referer=None, user_agent=None) is None
        assert calls == ["https://e.com/missing.jpg"]

    asyncio.run(run())


def test_store_fetches_once_and_caches(photo):
    calls = []

    async def fetcher(url, referer, ua):
        calls.append((url, referer, ua))
        return photo

    async def run():
        store = ImageStore(HostGuard(allow_private=True), fetcher=fetcher)
        for q in ("low", "mid", "low"):
            await store.variant("https://e.com/a.jpg", q, 1170, referer="https://e.com/", user_agent="UA")
        assert calls == [("https://e.com/a.jpg", "https://e.com/", "UA")]

    asyncio.run(run())


# ---------------------------------------------------------------- HTTP での取得


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        status, headers, body = self.routes.get(self.path, (404, {}, b""))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture(scope="module")
def server(photo):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    _Handler.routes = {
        "/a.jpg": (200, {"Content-Type": "image/jpeg"}, photo.body),
        "/redirect": (302, {"Location": "/a.jpg"}, b""),
        "/to-blocked": (302, {"Location": "http://blocked.test/a.jpg"}, b""),
        "/page.html": (200, {"Content-Type": "text/html"}, b"<script>alert(1)</script>"),
        "/huge.jpg": (200, {"Content-Type": "image/jpeg"}, b"x" * 2048),
    }
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield base
    server.shutdown()


def fetch(base: str, path: str, **kwargs) -> ImageData | None:
    store = ImageStore(HostGuard(allow_private=True, block_domains=["blocked.test"]), **kwargs)
    return asyncio.run(store.variant(base + path, "orig", 1170, referer=None, user_agent=None))


def test_http_fetch_follows_redirect(server, photo):
    assert fetch(server, "/redirect").body == photo.body


def test_http_fetch_checks_redirect_target(server):
    assert fetch(server, "/to-blocked") is None


def test_http_fetch_rejects_non_image(server):
    assert fetch(server, "/page.html") is None


def test_http_fetch_rejects_too_large(server):
    assert fetch(server, "/huge.jpg", max_image_bytes=1024) is None


def test_http_fetch_rejects_private_address_by_default(server):
    store = ImageStore(HostGuard())
    assert asyncio.run(store.variant(server + "/a.jpg", "orig", 1170, referer=None, user_agent=None)) is None
