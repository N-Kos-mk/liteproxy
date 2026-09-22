"""Playwright で実ページを描画し、transform.js で軽量 HTML へ変換する。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..config import BrowserConfig, RenderConfig
from ..security import HostGuard
from ..urls import FORM_ACTION_FIELD, FORM_CHARSET_FIELD, FORM_PATH, PROXY_PATH

log = logging.getLogger(__name__)

_INJECT = Path(__file__).parent / "inject"
TRANSFORM_JS = (_INJECT / "transform.js").read_text(encoding="utf-8")

# 遅延読み込みの画像などを発火させるため、ページ末尾まで順にスクロールしてから先頭へ戻す
SCROLL_JS = """async ({ maxSteps, delay }) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  let y = 0;
  for (let i = 0; i < maxSteps; i++) {
    y += innerHeight;
    scrollTo(0, y);
    await sleep(delay);
    if (y + innerHeight >= document.documentElement.scrollHeight) break;
  }
  scrollTo(0, 0);
  await sleep(delay);
}"""

# 画像の固有サイズを測るため、寸法の属性を持たない読み込み中の画像を待つ（上限あり）
WAIT_IMAGES_JS = """async (timeout) => {
  const pending = [...document.images].filter(
    (i) => !i.complete && !(i.getAttribute('width') && i.getAttribute('height')));
  if (!pending.length) return 0;
  await Promise.race([
    Promise.all(pending.map((i) => new Promise((r) => {
      i.addEventListener('load', r, { once: true });
      i.addEventListener('error', r, { once: true });
    }))),
    new Promise((r) => setTimeout(r, timeout)),
  ]);
  return pending.length;
}"""

# スマホへ送らず、描画結果の寸法にもほぼ影響しないため、PC 側でも取得しない
BLOCKED_RESOURCE_TYPES = frozenset({"font", "media", "texttrack", "manifest"})
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# 描画の段階。読み込み中画面（templates.loader_page）の表示と対応する
STAGE_QUEUED = "queued"  # 同時描画数の上限に達していて順番待ち
STAGE_FETCHING = "fetching"  # HTML を取得中
STAGE_RUNNING = "running"  # ページの JS・CSS の読み込みが落ち着くのを待機中
STAGE_SCROLLING = "scrolling"  # 遅延読み込みの画像などを読み込ませるためにスクロール中
STAGE_TRANSFORMING = "transforming"  # 軽量 HTML へ変換中
StageCallback = Callable[[str], None]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int
    dpr: float
    dark: bool


@dataclass
class Snapshot:
    url: str  # リダイレクト後の最終 URL
    title: str
    html: str
    pc_bytes: int  # PC 側で実際に取得したバイト数（ヘッダー込み・圧縮後）
    requests: int
    blocked: int
    css_bytes: int
    elapsed_ms: int
    transform_ms: int  # elapsed_ms のうち transform.js の実行時間


@dataclass
class NonHtml:
    url: str
    content_type: str
    size: int | None


class RenderError(Exception):
    """ページを描画できなかった。メッセージはそのまま利用者に表示する。"""


class _NetStats:
    def __init__(self) -> None:
        self.bytes = 0
        self.requests = 0
        self.blocked = 0

    def on_finished(self, event: dict) -> None:
        self.bytes += int(event.get("encodedDataLength") or 0)
        self.requests += 1


class _Inflight:
    """描画内容に影響する通信（メインフレームの JS・CSS・XHR 等）が落ち着いたかを見る。

    load イベントや networkidle は画像や広告 iframe の読み込みも待つため使わない。
    画像の寸法は属性から分かることが多く、分からないものだけ別途待つ。
    """

    TYPES = frozenset({"document", "stylesheet", "script", "xhr", "fetch"})

    def __init__(self, page: Page) -> None:
        self._page = page
        self._pending: set[Request] = set()
        self._changed = time.monotonic()

    def on_request(self, request: Request) -> None:
        try:
            main = request.frame == self._page.main_frame
        except PlaywrightError:
            return
        if main and request.resource_type in self.TYPES:
            self._pending.add(request)
            self._changed = time.monotonic()

    def on_done(self, request: Request) -> None:
        if request in self._pending:
            self._pending.discard(request)
            self._changed = time.monotonic()

    async def wait(self, quiet_ms: int, timeout_ms: int) -> None:
        """quiet_ms の間通信が途切れるまで待つ。ロングポーリング等に備えて timeout_ms で打ち切る。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if not self._pending and time.monotonic() - self._changed >= quiet_ms / 1000:
                return
            await asyncio.sleep(0.05)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else "不明なエラー"


async def _read_css(response: Response, sink: dict[str, str]) -> None:
    try:
        text = await response.text()
    except PlaywrightError:
        return
    # CSSOM 側はリダイレクト前の URL で参照するため、経由した URL すべてに対応付ける
    request = response.request
    while request is not None:
        sink[request.url] = text
        request = request.redirected_from


class BrowserPool:
    """ヘッドレス Chrome を 1 つ起動しておき、要求ごとに独立したコンテキストで描画する。"""

    def __init__(self, browser_cfg: BrowserConfig, render_cfg: RenderConfig, guard: HostGuard) -> None:
        self._cfg = browser_cfg
        self._render_cfg = render_cfg
        self._guard = guard
        self._sem = asyncio.Semaphore(browser_cfg.max_concurrency)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._launch_lock = asyncio.Lock()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        await self._launch()

    async def stop(self) -> None:
        with contextlib.suppress(PlaywrightError):
            if self._browser is not None:
                await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._browser = None
        self._pw = None

    async def _launch(self) -> None:
        assert self._pw is not None
        self._browser = await self._pw.chromium.launch(channel=self._cfg.channel or None, headless=True)
        log.info("ブラウザを起動しました (%s %s)", self._cfg.channel or "chromium", self._browser.version)

    async def _ensure_browser(self) -> Browser:
        if self._pw is None:
            raise RuntimeError("BrowserPool.start() が呼ばれていません")
        async with self._launch_lock:
            if self._browser is None or not self._browser.is_connected():
                log.warning("ブラウザとの接続が切れていたため再起動します")
                await self._launch()
        assert self._browser is not None
        return self._browser

    async def render(
        self,
        url: str,
        viewport: Viewport,
        user_agent: str | None,
        on_stage: StageCallback | None = None,
    ) -> Snapshot | NonHtml:
        """on_stage には処理の段階（STAGE_*）が順に渡される。読み込み中画面の表示に使う。"""
        stage = on_stage or (lambda _: None)
        stage(STAGE_QUEUED)
        async with self._sem:
            stage(STAGE_FETCHING)
            started = time.perf_counter()
            browser = await self._ensure_browser()
            # スマホと同じ画面条件で描画し、レイアウト（寸法・メディアクエリ）を一致させる
            context = await browser.new_context(
                viewport={"width": viewport.width, "height": viewport.height},
                device_scale_factor=viewport.dpr,
                is_mobile=True,
                has_touch=True,
                user_agent=user_agent or DEFAULT_USER_AGENT,
                color_scheme="dark" if viewport.dark else "light",
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                service_workers="block",
            )
            try:
                return await self._render(context, url, started, stage)
            finally:
                with contextlib.suppress(PlaywrightError):
                    await context.close()

    async def _render(
        self, context: BrowserContext, url: str, started: float, stage: StageCallback
    ) -> Snapshot | NonHtml:
        net = _NetStats()
        css_texts: dict[str, str] = {}
        css_tasks: list[asyncio.Future] = []

        async def route(route: Route) -> None:
            request = route.request
            try:
                if request.resource_type in BLOCKED_RESOURCE_TYPES or not await self._guard.allowed(request.url):
                    net.blocked += 1
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()
            except PlaywrightError:
                pass  # ページを閉じた後に届いた要求

        def on_response(response: Response) -> None:
            if response.request.resource_type == "stylesheet":
                css_tasks.append(asyncio.ensure_future(_read_css(response, css_texts)))

        await context.route("**/*", route)
        page = await context.new_page()
        inflight = _Inflight(page)
        page.on("request", inflight.on_request)
        page.on("requestfinished", inflight.on_done)
        page.on("requestfailed", inflight.on_done)
        page.on("response", on_response)
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")
        cdp.on("Network.loadingFinished", net.on_finished)

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._cfg.nav_timeout_ms)
        except PlaywrightTimeoutError as e:
            raise RenderError("ページの読み込みがタイムアウトしました。") from e
        except PlaywrightError as e:
            if "Download is starting" in e.message:
                return NonHtml(url, "application/octet-stream", None)
            raise RenderError(f"ページを開けませんでした: {_first_line(e.message)}") from e

        if response is not None:
            ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in HTML_TYPES:
                length = response.headers.get("content-length", "")
                return NonHtml(page.url, ctype, int(length) if length.isdigit() else None)
        # リダイレクトで LAN 内へ誘導された場合に内容を返さない
        if not await self._guard.allowed(page.url):
            raise RenderError("転送先の URL は開けません。")

        cfg = self._cfg
        stage(STAGE_RUNNING)
        await inflight.wait(cfg.quiet_ms, cfg.settle_timeout_ms)
        stage(STAGE_SCROLLING)
        try:
            await page.evaluate(SCROLL_JS, {"maxSteps": cfg.scroll_max_steps, "delay": cfg.scroll_delay_ms})
            # スクロールで始まった追加読み込みと、寸法の分からない画像を同時に待つ
            await asyncio.gather(
                inflight.wait(cfg.quiet_ms, cfg.image_wait_ms),
                page.evaluate(WAIT_IMAGES_JS, cfg.image_wait_ms),
            )
        except PlaywrightError as e:
            log.debug("スクロール中にエラー: %s", _first_line(e.message))
        if css_tasks:
            await asyncio.gather(*css_tasks, return_exceptions=True)

        origin = _origin(page.url)
        rc = self._render_cfg
        args = {
            "proxyPath": PROXY_PATH,
            "formPath": FORM_PATH,
            "formActionField": FORM_ACTION_FIELD,
            "formCharsetField": FORM_CHARSET_FIELD,
            "cssTexts": {u: t for u, t in css_texts.items() if _origin(u) != origin},
            "removeSelectors": rc.remove_selectors,
            "maxInlineSvg": rc.max_inline_svg,
            "maxDataUri": rc.max_data_uri,
            "pruneClasses": rc.prune_classes,
        }
        stage(STAGE_TRANSFORMING)
        transform_started = time.perf_counter()
        try:
            result = await page.evaluate(TRANSFORM_JS, args)
        except PlaywrightError as e:
            raise RenderError(f"ページの変換に失敗しました: {_first_line(e.message)}") from e
        transform_ms = round((time.perf_counter() - transform_started) * 1000)

        return Snapshot(
            url=result["url"],
            title=result["title"],
            html=result["html"],
            pc_bytes=net.bytes,
            requests=net.requests,
            blocked=net.blocked,
            css_bytes=result["cssBytes"],
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            transform_ms=transform_ms,
        )
