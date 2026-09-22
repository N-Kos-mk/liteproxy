"""FastAPI アプリ。スマホからの要求を受けて PC 側で描画し、軽量化した HTML を返す。"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import quote_plus

import jwt
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import templates
from .browser import BrowserPool, NonHtml, RenderError, Snapshot, Viewport
from .config import BrowserConfig, Config
from .render import layout
from .security import AccessVerifier, HostGuard
from .stats import StatsLog, human
from .urls import (
    FORM_ACTION_FIELD,
    FORM_CHARSET_FIELD,
    build_form_target,
    normalize_input,
    proxy_url,
    unwrap_redirector,
)

log = logging.getLogger(__name__)

# 変換漏れがあってもスマホ側で外部への通信が起きないようにする安全網
_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
    "frame-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


class Renderer(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def render(self, url: str, viewport: Viewport, user_agent: str | None) -> Snapshot | NonHtml: ...


def viewport_from_cookie(value: str | None, cfg: BrowserConfig) -> Viewport:
    """lp_env Cookie（幅_高さ_DPR_ダークモード）から描画条件を作る。"""
    default = Viewport(cfg.default_width, cfg.default_height, cfg.default_dpr, False)
    if not value:
        return default
    try:
        w, h, dpr, dark = value.split("_")
        return Viewport(
            width=min(max(int(w), 240), 1280),
            height=min(max(int(h), 320), 2000),
            dpr=min(max(float(dpr), 1.0), 4.0),
            dark=dark == "1",
        )
    except ValueError:
        return default


def html_response(
    body: str, *, nonce: str, status: int = 200, max_age: int = 0, language: str | None = "ja"
) -> HTMLResponse:
    """language は liteproxy 自身のページの言語。中継したページでは元の言語に任せるため None にする。"""
    headers = {
        "Content-Security-Policy": _CSP.format(nonce=nonce),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        # 戻る操作で再取得しないよう、スマホのブラウザに短時間キャッシュさせる
        "Cache-Control": f"private, max-age={max_age}" if max_age else "no-store",
    }
    if language:
        headers["Content-Language"] = language
    return HTMLResponse(body, status_code=status, headers=headers)


def create_app(
    config: Config,
    *,
    renderer: Renderer | None = None,
    verifier: AccessVerifier | None = None,
) -> FastAPI:
    guard = HostGuard(allow_private=config.network.allow_private, block_domains=config.network.block_domains)
    pool: Renderer = renderer or BrowserPool(config.browser, config.render, guard)
    stats_log = StatsLog(config.log.stats_file)
    if verifier is None and config.access.enabled:
        verifier = AccessVerifier(config.access.team_domain, config.access.aud, config.access.allowed_emails)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await pool.start()
        try:
            yield
        finally:
            await pool.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Cloudflare 経由ならエッジでも圧縮されるが、LAN 内から直接使う場合に備えて圧縮しておく
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    if verifier is not None:
        access = verifier

        @app.middleware("http")
        async def require_access(request: Request, call_next):
            token = request.headers.get(AccessVerifier.HEADER)
            if not token:
                return PlainTextResponse("Forbidden", status_code=403)
            try:
                await run_in_threadpool(access.verify, token)
            except jwt.PyJWTError as e:
                log.warning("Access JWT の検証に失敗: %s", e)
                return PlainTextResponse("Forbidden", status_code=403)
            return await call_next(request)

    @app.get("/")
    async def home() -> HTMLResponse:
        nonce = secrets.token_urlsafe(12)
        return html_response(templates.home(nonce), nonce=nonce)

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204, headers={"Cache-Control": "public, max-age=31536000"})

    @app.get("/s")
    async def search(q: str = "") -> Response:
        if not q.strip():
            return RedirectResponse("/", status_code=303)
        target = config.search.url.replace("{q}", quote_plus(q.strip()))
        return RedirectResponse(proxy_url(target), status_code=303)

    @app.get("/p")
    async def proxy(request: Request, u: str = "") -> Response:
        nonce = secrets.token_urlsafe(12)
        target = normalize_input(u)
        if target is None:
            if u.strip():
                return RedirectResponse(f"/s?q={quote_plus(u.strip())}", status_code=303)
            return RedirectResponse("/", status_code=303)
        target = unwrap_redirector(target)
        if not await guard.allowed(target):
            message = "このURLは開けません（内部ネットワーク宛て、またはブロック対象のドメインです）。"
            return html_response(templates.error_page(message, nonce), nonce=nonce, status=403)

        viewport = viewport_from_cookie(request.cookies.get("lp_env"), config.browser)
        try:
            result = await pool.render(target, viewport, request.headers.get("user-agent"))
        except RenderError as e:
            return html_response(templates.error_page(str(e), nonce, target=target), nonce=nonce, status=502)

        if isinstance(result, NonHtml):
            size = human(result.size) if result.size is not None else "不明"
            page = templates.non_html_page(result.url, result.content_type, size, nonce)
            return html_response(page, nonce=nonce)

        html, stats = layout.finalize(result, nonce)
        stats_log.write(stats)
        return html_response(html, nonce=nonce, max_age=300, language=None)

    @app.get("/f")
    async def form_get(request: Request) -> Response:
        action = ""
        charset = "utf-8"
        fields: list[tuple[str, str]] = []
        for key, value in request.query_params.multi_items():
            if key == FORM_ACTION_FIELD:
                action = value
            elif key == FORM_CHARSET_FIELD:
                charset = value
            else:
                fields.append((key, value))
        if normalize_input(action) is None:
            nonce = secrets.token_urlsafe(12)
            return html_response(templates.error_page("フォームの送信先が不正です。", nonce), nonce=nonce, status=400)
        return RedirectResponse(proxy_url(build_form_target(action, fields, charset)), status_code=303)

    @app.post("/f")
    async def form_post() -> Response:
        nonce = secrets.token_urlsafe(12)
        message = "POST で送信するフォームにはまだ対応していません。"
        return html_response(templates.error_page(message, nonce), nonce=nonce, status=501)

    return app
