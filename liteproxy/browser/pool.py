"""Playwright で実ページを描画し、transform.js で軽量 HTML へ変換する。

描画したページは閉じずにセッションとして保持する。スマホでタップされた要素は PC 側の
本物のページでタップし直し、変化した部分だけを返す（act）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..config import BrowserConfig, RenderConfig, SessionConfig
from ..media.image import ImageData, ImageStore
from ..security import HostGuard
from ..urls import FORM_ACTION_FIELD, FORM_CHARSET_FIELD, FORM_PATH, PROXY_PATH

log = logging.getLogger(__name__)

_INJECT = Path(__file__).parent / "inject"
TRANSFORM_JS = (_INJECT / "transform.js").read_text(encoding="utf-8")

# ページの JS より先に実行し、クリック系の処理が登録された要素を記録する（タップ可能かの判定に使う）
LISTENER_JS = """(() => {
  const targets = new WeakSet();
  const TYPES = new Set(['click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup', 'touchstart', 'touchend']);
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function (type, listener, options) {
    try {
      if (TYPES.has(type) && this instanceof Element) targets.add(this);
    } catch (e) {}
    return add.call(this, type, listener, options);
  };
  const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'onclick');
  if (desc && desc.set) {
    Object.defineProperty(HTMLElement.prototype, 'onclick', {
      ...desc,
      set(value) {
        if (value) targets.add(this);
        return desc.set.call(this, value);
      },
    });
  }
  Object.defineProperty(window, __LP_NS__ + '_t', { value: targets });
})();"""

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

# 置き換え対象になる <img> の URL（描画時に既定の画質へ変換する対象）
IMAGE_URLS_JS = """() => [...new Set([...document.images]
  .map((i) => i.currentSrc || i.src)
  .filter((u) => u && /^https?:/.test(u)))]"""

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
    image_ms: int = 0  # elapsed_ms のうち画像を既定の画質へ変換した時間
    image_count: int = 0  # サイズを表示した画像の数
    image_quality: str | None = None  # 表示したサイズの画質
    session_id: str | None = None  # PC 側で保持しているページ（操作の中継に使う）
    rev: int = 0  # この HTML の版。操作の差分はこの版からの変化として返す


@dataclass
class NonHtml:
    url: str
    content_type: str
    size: int | None


@dataclass
class ActResult:
    """スマホでのタップを PC 側で再現した結果。"""

    status: str  # ok / navigated / popup / expired / reload / error
    rev: int = 0
    ops: list = field(default_factory=list)  # DOM の差分（transform.js の sync() を参照）
    css: list = field(default_factory=list)  # 新たに必要になった CSS
    snapshot: Snapshot | None = None  # navigated: 遷移先のページ
    url: str | None = None  # popup: 新しいタブで開かれた URL
    message: str | None = None  # error: 利用者に表示する内容


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

    def mark(self) -> tuple[int, int, int]:
        return (self.bytes, self.requests, self.blocked)


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

    def idle_ms(self) -> float:
        return 0.0 if self._pending else (time.monotonic() - self._changed) * 1000

    def loading_document(self) -> bool:
        return any(r.resource_type == "document" for r in self._pending)

    async def wait(self, quiet_ms: int, timeout_ms: int) -> None:
        """quiet_ms の間通信が途切れるまで待つ。ロングポーリング等に備えて timeout_ms で打ち切る。"""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.idle_ms() >= quiet_ms:
                return
            await asyncio.sleep(0.05)


class Session:
    """描画後も保持している PC 側のページ。"""

    def __init__(self, context: BrowserContext, viewport: Viewport) -> None:
        self.id = secrets.token_urlsafe(12)
        self.ns = "__lp_" + secrets.token_hex(6)  # ページ内に置く API の名前
        self.context = context
        self.viewport = viewport
        self.page: Page | None = None
        self.inflight: _Inflight | None = None
        self.net = _NetStats()
        self.css_texts: dict[str, str] = {}
        self.css_sent: set[str] = set()
        self.css_tasks: list[asyncio.Future] = []
        self.image_tasks: list[asyncio.Future] = []
        self.popups: list[Page] = []
        self.navigations = 0  # メインフレームの遷移（pushState を含む）の回数
        self.lock = asyncio.Lock()
        self.rev = 0
        self.last_used = time.monotonic()

    def on_navigated(self, frame: Frame) -> None:
        if self.page is not None and frame == self.page.main_frame:
            self.navigations += 1

    def on_page(self, page: Page) -> None:
        if page is not self.page:
            self.popups.append(page)

    async def close(self) -> None:
        with contextlib.suppress(PlaywrightError):
            await self.context.close()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else "不明なエラー"


async def _read_image(response: Response, store: ImageStore) -> None:
    """Chrome が取得した画像を元データとして保持し、タップ時に再取得しないで済むようにする。"""
    ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    length = response.headers.get("content-length", "")
    if response.status != 200 or not ctype.startswith("image/"):
        return
    if length.isdigit() and int(length) > store.max_image_bytes:
        return
    try:
        body = await response.body()
    except PlaywrightError:
        return
    if len(body) > store.max_image_bytes:
        return
    data = ImageData(body, ctype)
    request = response.request
    while request is not None:
        store.add_original(request.url, data)
        request = request.redirected_from


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


async def _drain(tasks: list[asyncio.Future]) -> None:
    if tasks:
        pending = list(tasks)
        tasks.clear()
        await asyncio.gather(*pending, return_exceptions=True)


class BrowserPool:
    """ヘッドレス Chrome を 1 つ起動しておき、ページごとに独立したコンテキストで描画する。"""

    def __init__(
        self,
        browser_cfg: BrowserConfig,
        render_cfg: RenderConfig,
        guard: HostGuard,
        images: ImageStore | None = None,
        session_cfg: SessionConfig | None = None,
    ) -> None:
        self._cfg = browser_cfg
        self._render_cfg = render_cfg
        self._guard = guard
        self._images = images
        self._session_cfg = session_cfg or SessionConfig()
        self._sem = asyncio.Semaphore(browser_cfg.max_concurrency)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._launch_lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._reaper: asyncio.Task | None = None
        self._closing: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        await self._launch()
        self._reaper = asyncio.create_task(self._reap())

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        for s in list(self._sessions.values()):
            await s.close()
        self._sessions.clear()
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
                self._sessions.clear()
                await self._launch()
        assert self._browser is not None
        return self._browser

    # ------------------------------------------------------------ 描画

    async def render(
        self,
        url: str,
        viewport: Viewport,
        user_agent: str | None,
        on_stage: StageCallback | None = None,
        image_quality: str | None = None,
    ) -> Snapshot | NonHtml:
        """on_stage には処理の段階（STAGE_*）が順に渡される。読み込み中画面の表示に使う。

        image_quality を渡すと、ページ内の画像をその画質へ変換しておき、画像の枠にサイズを表示する。
        """
        stage = on_stage or (lambda _: None)
        stage(STAGE_QUEUED)
        async with self._sem:
            stage(STAGE_FETCHING)
            started = time.perf_counter()
            browser = await self._ensure_browser()
            session = await self._new_session(browser, viewport, user_agent)
            keep = False
            try:
                non_html = await self._open(session, url)
                if non_html is not None:
                    return non_html
                snap = await self._capture(session, stage, image_quality, started, (0, 0, 0))
                if self._session_cfg.enabled:
                    keep = True
                    snap.session_id = session.id
                    self._register(session)
                return snap
            finally:
                if not keep:
                    await session.close()

    async def _new_session(self, browser: Browser, viewport: Viewport, user_agent: str | None) -> Session:
        # スマホと同じ画面条件で描画し、レイアウト（寸法・メディアクエリ）を一致させる。
        # bypass_csp は、Trusted Types を使うページでもミラー（DOMParser）を作れるようにするため
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
            bypass_csp=True,
        )
        s = Session(context, viewport)
        images = self._images

        async def route(route: Route) -> None:
            request = route.request
            try:
                if request.resource_type in BLOCKED_RESOURCE_TYPES or not await self._guard.allowed(request.url):
                    s.net.blocked += 1
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()
            except PlaywrightError:
                pass  # ページを閉じた後に届いた要求

        def on_response(response: Response) -> None:
            kind = response.request.resource_type
            if kind == "stylesheet":
                s.css_tasks.append(asyncio.ensure_future(_read_css(response, s.css_texts)))
            elif kind == "image" and images is not None:
                s.image_tasks.append(asyncio.ensure_future(_read_image(response, images)))

        await context.route("**/*", route)
        await context.add_init_script(LISTENER_JS.replace("__LP_NS__", json.dumps(s.ns)))
        page = await context.new_page()
        s.page = page
        s.inflight = _Inflight(page)
        page.on("request", s.inflight.on_request)
        page.on("requestfinished", s.inflight.on_done)
        page.on("requestfailed", s.inflight.on_done)
        page.on("response", on_response)
        page.on("framenavigated", s.on_navigated)
        context.on("page", s.on_page)
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")
        cdp.on("Network.loadingFinished", s.net.on_finished)
        return s

    async def _open(self, s: Session, url: str) -> NonHtml | None:
        page = s.page
        assert page is not None
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
        return None

    async def _capture(
        self,
        s: Session,
        stage: StageCallback,
        image_quality: str | None,
        started: float,
        base: tuple[int, int, int],
    ) -> Snapshot:
        """読み込まれたページの状態を落ち着かせてから、軽量 HTML に変換する。"""
        page = s.page
        assert page is not None and s.inflight is not None
        cfg = self._cfg
        stage(STAGE_RUNNING)
        await s.inflight.wait(cfg.quiet_ms, cfg.settle_timeout_ms)
        stage(STAGE_SCROLLING)
        try:
            await page.evaluate(SCROLL_JS, {"maxSteps": cfg.scroll_max_steps, "delay": cfg.scroll_delay_ms})
            # スクロールで始まった追加読み込みと、寸法の分からない画像を同時に待つ
            await asyncio.gather(
                s.inflight.wait(cfg.quiet_ms, cfg.image_wait_ms),
                page.evaluate(WAIT_IMAGES_JS, cfg.image_wait_ms),
            )
        except PlaywrightError as e:
            log.debug("スクロール中にエラー: %s", _first_line(e.message))
        await _drain(s.css_tasks)

        stage(STAGE_TRANSFORMING)
        image_sizes: dict[str, int] = {}
        image_started = time.perf_counter()
        await _drain(s.image_tasks)
        if self._images is not None and image_quality:
            try:
                urls = await page.evaluate(IMAGE_URLS_JS)
            except PlaywrightError:
                urls = []
            image_sizes = await self._images.prepare(urls, image_quality, self._screen_px(s))
        image_ms = round((time.perf_counter() - image_started) * 1000)

        origin = _origin(page.url)
        cross = {u: t for u, t in s.css_texts.items() if _origin(u) != origin}
        s.css_sent = set(cross)
        rc = self._render_cfg
        args = {
            "ns": s.ns,
            "revBase": s.rev + 1,  # 以前の版の HTML からの操作を受け付けないよう、版を進める
            "proxyPath": PROXY_PATH,
            "formPath": FORM_PATH,
            "formActionField": FORM_ACTION_FIELD,
            "formCharsetField": FORM_CHARSET_FIELD,
            "cssTexts": cross,
            "removeSelectors": rc.remove_selectors,
            "maxInlineSvg": rc.max_inline_svg,
            "maxDataUri": rc.max_data_uri,
            "pruneClasses": rc.prune_classes,
            "imageSizes": image_sizes,
        }
        transform_started = time.perf_counter()
        try:
            result = await page.evaluate(TRANSFORM_JS, args)
        except PlaywrightError as e:
            raise RenderError(f"ページの変換に失敗しました: {_first_line(e.message)}") from e
        transform_ms = round((time.perf_counter() - transform_started) * 1000)
        s.rev = int(result["rev"])

        bytes0, requests0, blocked0 = base
        return Snapshot(
            url=result["url"],
            title=result["title"],
            html=result["html"],
            pc_bytes=s.net.bytes - bytes0,
            requests=s.net.requests - requests0,
            blocked=s.net.blocked - blocked0,
            css_bytes=result["cssBytes"],
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            transform_ms=transform_ms,
            image_ms=image_ms,
            image_count=len(image_sizes),
            image_quality=image_quality if image_sizes else None,
            rev=s.rev,
        )

    @staticmethod
    def _screen_px(s: Session) -> int:
        return round(s.viewport.width * s.viewport.dpr)

    # ------------------------------------------------------------ 操作の中継

    async def act(
        self,
        session_id: str,
        rev: int,
        path: list[int],
        image_quality: str | None = None,
        kind: str = "click",
        fields: list[list[str]] | None = None,
        submitter: list[int] | None = None,
    ) -> ActResult:
        """スマホでの操作（パスで指定）を PC 側で再現し、変化を差分として返す。

        kind="submit" では、スマホで入力された値（fields）を PC 側のフォームへ入れてから送信する。
        PC 側のページが持つ Cookie や、フォームに埋め込まれたトークンをそのまま使える。
        """
        s = self._sessions.get(session_id)
        if s is None or s.page is None or s.page.is_closed():
            return ActResult("expired")
        async with s.lock:
            s.last_used = time.monotonic()
            if rev != s.rev:
                return ActResult("reload")  # スマホ側の HTML が PC 側の状態と食い違っている
            page = s.page
            started = time.perf_counter()
            base = s.net.mark()
            url_before, navigations_before = page.url, s.navigations
            s.popups.clear()
            ns = json.dumps(s.ns)

            if kind == "submit":
                try:
                    filled = await page.evaluate(
                        f"(a) => window[{ns}] ? window[{ns}].fill(a) : false", {"path": path, "fields": fields or []}
                    )
                except PlaywrightError:
                    return ActResult("reload")
                if not filled:
                    return ActResult("reload")
                # 送信ボタンが分かる場合はそれを押す（ボタンの名前と値、JS の処理も元のまま動く）
                if submitter is not None:
                    failed = await self._tap(page, s.ns, submitter)
                    if failed is not None:
                        return failed
                else:
                    try:
                        await page.evaluate(f"(p) => window[{ns}].requestSubmit(p)", path)
                    except PlaywrightError as e:
                        log.info("フォームを送信できません: %s", _first_line(e.message))
                        return ActResult("error", message="フォームを送信できませんでした。")
            else:
                failed = await self._tap(page, s.ns, path)
                if failed is not None:
                    return failed

            await self._settle_action(s, navigations_before)

            if s.popups:
                return await self._popup_result(s)
            if s.navigations != navigations_before or page.url != url_before:
                # 画面遷移（pushState を含む）: 遷移先の状態を新しいページとして取り込む
                with contextlib.suppress(PlaywrightTimeoutError):
                    await page.wait_for_load_state("domcontentloaded", timeout=self._cfg.nav_timeout_ms)
                if not await self._guard.allowed(page.url):
                    return ActResult("error", message="転送先の URL は開けません。")
                try:
                    snap = await self._capture(s, lambda _: None, image_quality, started, base)
                except RenderError as e:
                    return ActResult("error", message=str(e))
                snap.session_id = s.id
                return ActResult("navigated", rev=s.rev, snapshot=snap)

            try:
                urls = await page.evaluate(f"() => window[{ns}].dirtyImages()")
                sizes: dict[str, int] = {}
                if self._images is not None and image_quality and urls:
                    await _drain(s.image_tasks)
                    sizes = await self._images.prepare(urls, image_quality, self._screen_px(s))
                await _drain(s.css_tasks)
                origin = _origin(page.url)
                new_css = {
                    u: t for u, t in s.css_texts.items() if _origin(u) != origin and u not in s.css_sent
                }
                data = await page.evaluate(
                    f"(a) => window[{ns}].sync(a)", {"imageSizes": sizes, "cssTexts": new_css}
                )
            except PlaywrightError as e:
                log.info("差分を取得できません: %s", _first_line(e.message))
                return ActResult("reload")
            s.css_sent.update(new_css)
            s.rev = int(data["rev"])
            return ActResult("ok", rev=s.rev, ops=data["ops"], css=data["css"])

    async def _tap(self, page: Page, ns_name: str, path: list[int]) -> ActResult | None:
        """パスが指す要素をタップする。問題があれば返す ActResult をそのまま応答に使う。"""
        ns = json.dumps(ns_name)
        try:
            handle = await page.evaluate_handle(f"(p) => window[{ns}] ? window[{ns}].resolve(p) : null", path)
        except PlaywrightError:
            return ActResult("reload")
        element = handle.as_element()
        if element is None:
            await handle.dispose()
            return ActResult("reload")
        try:
            try:
                await element.tap(timeout=self._session_cfg.tap_timeout_ms)
            except PlaywrightError:
                # 他の要素に覆われている・見えていない場合でも、クリックのイベントだけは送る
                await element.dispatch_event("click")
        except PlaywrightError as e:
            log.info("タップを再現できません: %s", _first_line(e.message))
            return ActResult("error", message="この要素はPC側で操作できませんでした。")
        finally:
            with contextlib.suppress(PlaywrightError):
                await handle.dispose()
        return None

    async def _settle_action(self, s: Session, navigations_before: int) -> None:
        """タップ後、DOM の変化と通信が落ち着くまで待つ。遷移が始まったら遷移の完了まで待つ。"""
        cfg = self._session_cfg
        page = s.page
        assert page is not None and s.inflight is not None
        ns = json.dumps(s.ns)
        deadline = time.monotonic() + cfg.settle_ms / 1000
        await asyncio.sleep(0.15)  # タップに反応する処理が動き始めるのを待つ
        while time.monotonic() < deadline:
            if s.popups or s.navigations != navigations_before:
                return
            if not s.inflight.loading_document():
                try:
                    quiet = await page.evaluate(f"() => window[{ns}] ? window[{ns}].quietMs() : 1e9")
                except PlaywrightError:
                    return  # 遷移で実行環境が入れ替わった
                if quiet >= cfg.quiet_ms and s.inflight.idle_ms() >= 100:
                    return
            await asyncio.sleep(0.05)
        # 遷移の読み込みが長い場合は、遷移が確定するまで待つ
        nav_deadline = time.monotonic() + self._cfg.nav_timeout_ms / 1000
        while s.inflight.loading_document() and s.navigations == navigations_before:
            if time.monotonic() > nav_deadline:
                return
            await asyncio.sleep(0.05)

    async def _popup_result(self, s: Session) -> ActResult:
        popup = s.popups[0]
        with contextlib.suppress(PlaywrightError):
            await popup.wait_for_load_state("commit", timeout=5000)
        url = popup.url
        for p in s.popups:
            with contextlib.suppress(PlaywrightError):
                await p.close()
        s.popups.clear()
        if url.startswith(("http://", "https://")):
            return ActResult("popup", rev=s.rev, url=url)
        return ActResult("ok", rev=s.rev)

    # ------------------------------------------------------------ セッションの管理

    def _register(self, s: Session) -> None:
        self._sessions[s.id] = s
        idle = sorted((x for x in self._sessions.values() if not x.lock.locked()), key=lambda x: x.last_used)
        while len(self._sessions) > max(1, self._session_cfg.max_sessions) and idle:
            old = idle.pop(0)
            if old is s:
                continue
            self._drop(old)

    def _drop(self, s: Session) -> None:
        self._sessions.pop(s.id, None)
        task = asyncio.create_task(s.close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(30)
            limit = self._session_cfg.idle_minutes * 60
            now = time.monotonic()
            for s in list(self._sessions.values()):
                if not s.lock.locked() and now - s.last_used > limit:
                    self._drop(s)
