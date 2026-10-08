"""起動時にブラウザを用意できなくても、アプリが動き続けることを確かめる。

ブラウザの起動に失敗して uvicorn が落ちると、cloudflared の転送先が消えて
Cloudflare が 502 を返してしまう。起動は落とさずに、次に使うときやり直す。
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Error as PlaywrightError

from liteproxy.browser.pool import BrowserPool
from liteproxy.config import BrowserConfig, RenderConfig
from liteproxy.security import HostGuard


class FakeBrowser:
    """_launch の代わりに差し込む、起動済みブラウザの代用。"""

    def is_connected(self) -> bool:
        return True

    async def close(self) -> None:
        pass


def make_pool() -> BrowserPool:
    return BrowserPool(BrowserConfig(), RenderConfig(), HostGuard())


def test_start_survives_launch_failure():
    """ブラウザを起動できなくても start() は例外を投げない。"""

    async def main():
        pool = make_pool()
        attempts = 0

        async def fail():
            nonlocal attempts
            attempts += 1
            raise PlaywrightError("launch failed")

        pool._launch = fail
        try:
            await pool.start()
            assert attempts == 1
            assert pool._browser is None
        finally:
            await pool.stop()

    asyncio.run(main())


def test_browser_is_launched_again_on_first_use():
    """起動時に失敗しても、次に使うときに起動し直す。"""

    async def main():
        pool = make_pool()
        attempts = 0

        async def flaky():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PlaywrightError("launch failed")
            pool._browser = FakeBrowser()

        pool._launch = flaky
        try:
            await pool.start()
            assert pool._browser is None
            await pool.ensure_ready()
            assert attempts == 2
            assert isinstance(pool._browser, FakeBrowser)
        finally:
            await pool.stop()

    asyncio.run(main())
