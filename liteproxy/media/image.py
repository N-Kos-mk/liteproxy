"""画像の縮小・再圧縮と、元画像・変換結果の短期保持。

画像はスマホでタップされたときだけ /i から配信する。PC 側で描画したときに取得済みの
画像は元データを保持しておき、既定の画質へは描画と同時に変換してサイズを表示する。
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx2 as httpx
from PIL import Image, ImageOps

from ..security import HostGuard

log = logging.getLogger(__name__)

# 画質ごとの縮小条件と WebP の品質。high の幅の上限は端末の画面の実ピクセル幅で決まる
QUALITY_LABELS = {"low": "低", "mid": "中", "high": "高", "orig": "原本"}
_LONG_SIDE = {"low": 320, "mid": 640}
_WEBP_QUALITY = {"low": 40, "mid": 55, "high": 75}

Image.MAX_IMAGE_PIXELS = 60_000_000  # 巨大な画像による過大なメモリ消費を防ぐ


@dataclass(frozen=True)
class ImageData:
    body: bytes
    content_type: str


def convert(data: ImageData, quality: str, screen_px: int) -> ImageData:
    """画質に合わせて縮小し WebP にする。変換できない・かえって大きくなる場合は元のまま返す。"""
    if quality == "orig" or quality not in _WEBP_QUALITY:
        return data
    try:
        with Image.open(io.BytesIO(data.body)) as img:
            if quality in _LONG_SIDE:
                side = _LONG_SIDE[quality]
                box = (side, side)
            else:
                box = (screen_px, screen_px * 10)
            img.draft("RGB", box)  # JPEG は縮小しながら読み込めるため速い
            img = ImageOps.exif_transpose(img)
            img.thumbnail(box, Image.Resampling.LANCZOS, reducing_gap=2.0)
            has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
            img = img.convert("RGBA" if has_alpha else "RGB")
            out = io.BytesIO()
            img.save(out, "WEBP", quality=_WEBP_QUALITY[quality], method=4)
    except Exception as e:  # noqa: BLE001  壊れた画像・未対応の形式（SVG など）
        log.debug("画像を変換できません: %s", e)
        return data
    body = out.getvalue()
    return ImageData(body, "image/webp") if len(body) < len(data.body) else data


class _Lru:
    """合計バイト数で上限を持つ LRU。"""

    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes
        self._size = 0
        self._items: OrderedDict[object, ImageData] = OrderedDict()

    def get(self, key: object) -> ImageData | None:
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
        return item

    def put(self, key: object, item: ImageData) -> None:
        if len(item.body) > self._max:
            return
        old = self._items.pop(key, None)
        if old is not None:
            self._size -= len(old.body)
        self._items[key] = item
        self._size += len(item.body)
        while self._size > self._max:
            _, dropped = self._items.popitem(last=False)
            self._size -= len(dropped.body)


Fetcher = Callable[[str, str | None, str | None], Awaitable[ImageData | None]]


class ImageStore:
    def __init__(
        self,
        guard: HostGuard,
        *,
        cache_bytes: int = 256 * 1024 * 1024,
        max_image_bytes: int = 20 * 1024 * 1024,
        fetcher: Fetcher | None = None,
    ) -> None:
        self._guard = guard
        self._max_image = max_image_bytes
        # 元画像と変換結果で上限を分け、変換結果が元画像に押し出されないようにする
        self._originals = _Lru(cache_bytes * 3 // 4)
        self._variants = _Lru(cache_bytes // 4)
        self._fetch = fetcher or self._fetch_http

    @property
    def max_image_bytes(self) -> int:
        return self._max_image

    def add_original(self, url: str, data: ImageData) -> None:
        self._originals.put(url, data)

    async def prepare(self, urls: list[str], quality: str, screen_px: int) -> dict[str, int]:
        """保持している元画像を既定の画質へ変換し、URL ごとの送信サイズを返す。"""
        sizes: dict[str, int] = {}

        async def one(url: str) -> None:
            original = self._originals.get(url)
            if original is not None:
                sizes[url] = len((await self._variant_from(url, original, quality, screen_px)).body)

        await asyncio.gather(*(one(u) for u in urls))
        return sizes

    async def variant(
        self, url: str, quality: str, screen_px: int, *, referer: str | None, user_agent: str | None
    ) -> ImageData | None:
        cached = self._variants.get(self._key(url, quality, screen_px))
        if cached is not None:
            return cached
        original = self._originals.get(url)
        if original is None:
            original = await self._fetch(url, referer, user_agent)
            if original is None:
                return None
            self._originals.put(url, original)
        return await self._variant_from(url, original, quality, screen_px)

    async def _variant_from(self, url: str, original: ImageData, quality: str, screen_px: int) -> ImageData:
        key = self._key(url, quality, screen_px)
        cached = self._variants.get(key)
        if cached is None:
            cached = await asyncio.to_thread(convert, original, quality, screen_px)
            self._variants.put(key, cached)
        return cached

    @staticmethod
    def _key(url: str, quality: str, screen_px: int) -> tuple[str, str, int]:
        return (url, quality, screen_px if quality == "high" else 0)

    async def _fetch_http(self, url: str, referer: str | None, user_agent: str | None) -> ImageData | None:
        """描画時に取得していなかった画像を取りに行く。リダイレクト先も LAN 宛てでないか確かめる。"""
        headers = {"Accept": "image/avif,image/webp,image/*;q=0.8"}
        if referer:
            headers["Referer"] = referer
        if user_agent:
            headers["User-Agent"] = user_agent
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            for _ in range(5):
                if not await self._guard.allowed(url):
                    return None
                async with client.stream("GET", url, headers=headers) as res:
                    if res.is_redirect and "location" in res.headers:
                        url = urljoin(url, res.headers["location"])
                        continue
                    ctype = res.headers.get("content-type", "").split(";")[0].strip().lower()
                    if res.status_code != 200 or not ctype.startswith("image/"):
                        return None
                    body = bytearray()
                    async for chunk in res.aiter_bytes():
                        body += chunk
                        if len(body) > self._max_image:
                            return None
                    return ImageData(bytes(body), ctype)
        return None
