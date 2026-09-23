"""FastAPI アプリ。スマホからの要求を受けて PC 側で描画し、軽量化した HTML を返す。"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import AsyncIterator
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, quote_plus

import jwt
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from . import templates
from .browser import BrowserPool, NonHtml, Snapshot, Viewport
from .config import BrowserConfig, Config
from .jobs import Job, Renderer, RenderJobs, RenderKey
from .media.image import QUALITY_LABELS, ImageStore
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

# 変換漏れがあってもスマホ側で外部への通信が起きないようにする安全網。
# 読み込めるのは liteproxy 自身の画像（/i）と JS（/static）だけにする
_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'self' 'nonce-{nonce}'; "
    "connect-src 'self'; frame-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
# /i は取得した画像をそのまま返すことがあるため、SVG 内のスクリプトなどが動かないようにする
_IMAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
# ホームと /app（シェル）だけ、PWA の manifest と Service Worker の登録を許可する
# （default-src 'none' はどちらにもフォールバックしないため明示が必要）
_PWA_CSP = _CSP + "; manifest-src 'self'; worker-src 'self'"
# 中継ページを iframe に埋め込めるよう、埋め込みが確認できたときだけ frame-ancestors を緩める。
# frame-src 'self' は既存の _CSP に既にある（元ページ内の iframe を残す機能のため）ので変更不要
_CSP_EMBEDDABLE = _CSP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
# ホーム（/）は /app のシェルが最初に iframe へ読み込む先でもあるため、埋め込み時だけ frame-ancestors を緩める
_PWA_CSP_EMBEDDABLE = _CSP_EMBEDDABLE + "; manifest-src 'self'; worker-src 'self'"

_CLIENT_JS = (Path(__file__).parent / "static" / "client.js").read_bytes()
# 内容が変わったら URL も変わるようにし、スマホ側には長期間キャッシュさせる
CLIENT_SRC = f"/static/client.js?v={hashlib.sha256(_CLIENT_JS).hexdigest()[:10]}"

_ICON_192 = (Path(__file__).parent / "static" / "icon-192.png").read_bytes()
_ICON_512 = (Path(__file__).parent / "static" / "icon-512.png").read_bytes()
ICON_192_SRC = f"/static/icon-192.png?v={hashlib.sha256(_ICON_192).hexdigest()[:10]}"
ICON_512_SRC = f"/static/icon-512.png?v={hashlib.sha256(_ICON_512).hexdigest()[:10]}"

_MANIFEST = json.dumps(
    {
        "name": "liteproxy",
        "short_name": "liteproxy",
        "start_url": "/app",
        "display": "standalone",
        "background_color": "#1f2328",
        "theme_color": "#1f2328",
        "icons": [
            {"src": ICON_192_SRC, "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": ICON_512_SRC, "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ],
    },
    ensure_ascii=False,
).encode("utf-8")

# ホーム画面（外枠）だけをキャッシュする。/p・/v・/a・/i・/s・/f などの中継結果は
# 古い内容をオフライン時に誤って返さないよう、対象に含めない。
_SW_ASSETS = ["/", "/app", CLIENT_SRC, "/manifest.json", ICON_192_SRC, ICON_512_SRC]
# 中身が変わったら Service Worker のテキスト自体も変わるようにし、ブラウザに再インストールさせる
_SW_CACHE = "lp-" + hashlib.sha256("".join(_SW_ASSETS).encode()).hexdigest()[:10]
_SERVICE_WORKER = f"""\
const CACHE = {json.dumps(_SW_CACHE)};
const ASSETS = {json.dumps(_SW_ASSETS)};
const ASSET_URLS = new Set(ASSETS.map((p) => new URL(p, self.location.origin).href));

self.addEventListener("install", (event) => {{
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k.startsWith("lp-")).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
}});

self.addEventListener("fetch", (event) => {{
  const req = event.request;
  if (req.method !== "GET" || !ASSET_URLS.has(req.url)) return;
  event.respondWith(
    fetch(req)
      .then((res) => {{
        if (res.ok) {{
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }}
        return res;
      }})
      .catch(() => caches.match(req))
  );
}});
""".encode("utf-8")


# 表示モード（layout: レイアウトを保つ / reader: 本文だけ）
MODES = ("layout", "reader")

# 読み込み中画面へ、段階に変化がなくてもこの間隔で生存通知を送る
LOADER_HEARTBEAT_SEC = 2.0


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


def is_embedded(request: Request) -> bool:
    """/app のシェルが iframe として埋め込んでいるかを、ブラウザが自動付与する（JS からは偽装できない）
    Fetch Metadata ヘッダーで判定する。無ければ安全側（今までどおりツールバー表示・埋め込み不可）に倒す。"""
    return (
        request.headers.get("sec-fetch-dest") == "iframe"
        and request.headers.get("sec-fetch-site") in ("same-origin", "none")
    )


def page_headers(
    *,
    nonce: str,
    cache_control: str = "no-store",
    language: str | None = "ja",
    csp: str | None = None,
    embed: bool = False,
) -> dict[str, str]:
    """language は liteproxy 自身のページの言語。中継したページでは元の言語に任せるため None にする。

    csp を渡すと既定の代わりに使う（home()/app() だけ manifest-src/worker-src を足すため）。
    embed は csp を渡さないときだけ効き、iframe に埋め込まれたときだけ frame-ancestors を緩める。
    """
    if csp is not None:
        resolved_csp = csp
    else:
        resolved_csp = (_CSP_EMBEDDABLE if embed else _CSP).format(nonce=nonce)
    headers = {
        "Content-Security-Policy": resolved_csp,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": cache_control,
    }
    if language:
        headers["Content-Language"] = language
    # embed の有無でレスポンス内容（frame-ancestors・ツールバーの表示）が変わるため、
    # スマホ側の HTTP キャッシュが埋め込み/非埋め込みの応答を取り違えないようにする
    if cache_control.startswith("private, max-age"):
        headers["Vary"] = "Sec-Fetch-Dest, Sec-Fetch-Site"
    return headers


def html_response(
    body: str,
    *,
    nonce: str,
    status: int = 200,
    max_age: int = 0,
    language: str | None = "ja",
    csp: str | None = None,
    embed: bool = False,
) -> HTMLResponse:
    # 戻る操作で再取得しないよう、スマホのブラウザに短時間キャッシュさせる
    cache_control = f"private, max-age={max_age}" if max_age else "no-store"
    headers = page_headers(nonce=nonce, cache_control=cache_control, language=language, csp=csp, embed=embed)
    return HTMLResponse(body, status_code=status, headers=headers)


def view_url(target: str, job_id: str) -> str:
    return f"/v?u={quote(target, safe='')}&j={job_id}"


async def loader_stream(job: Job, target: str, nonce: str) -> AsyncIterator[str]:
    """読み込み中画面を送り、描画の段階が変わるたび（変化がなくても一定間隔で）進捗を追記する。"""
    yield templates.loader_page(target, nonce)
    seen = -1
    while True:
        seen = await job.wait_change(seen, LOADER_HEARTBEAT_SEC)
        if job.error is not None:
            yield templates.loader_update("error", job.elapsed_ms(), nonce, {"message": job.error})
            return
        if job.result is not None:
            extra = {"next": view_url(target, job.id), "size": job.sent_bytes or 0}
            yield templates.loader_update("done", job.elapsed_ms(), nonce, extra)
            return
        yield templates.loader_update(job.stage, job.elapsed_ms(), nonce)


def create_app(
    config: Config,
    *,
    renderer: Renderer | None = None,
    verifier: AccessVerifier | None = None,
) -> FastAPI:
    guard = HostGuard(allow_private=config.network.allow_private, block_domains=config.network.block_domains)
    mb = 1024 * 1024
    images = ImageStore(guard, cache_bytes=config.image.cache_mb * mb, max_image_bytes=config.image.max_image_mb * mb)
    pool: Renderer = renderer or BrowserPool(config.browser, config.render, guard, images, config.session)
    default_quality = config.image.default_quality
    stats_log = StatsLog(config.log.stats_file)
    if verifier is None and config.access.enabled:
        verifier = AccessVerifier(config.access.team_domain, config.access.aud, config.access.allowed_emails)

    def on_complete(job: Job, result: Snapshot | NonHtml) -> int | None:
        if isinstance(result, NonHtml):
            return None
        _, stats = layout.finalize(result, nonce="", default_quality=default_quality, client_src=CLIENT_SRC)
        stats_log.write(stats)
        return stats.gzip_bytes

    jobs = RenderJobs(pool, on_complete=on_complete)

    def page_mode(request: Request, requested: str = "") -> str:
        """表示モードは、URL の m、Cookie（lp_m）、設定の既定値の順に決める。"""
        if requested in MODES:
            return requested
        cookie = request.cookies.get("lp_m", "")
        return cookie if cookie in MODES else config.render.default_mode

    def image_quality(request: Request) -> str:
        q = request.cookies.get("lp_q", "")
        return q if q in QUALITY_LABELS else default_quality

    def render_key(request: Request, target: str, mode: str | None = None) -> RenderKey:
        viewport = viewport_from_cookie(request.cookies.get("lp_env"), config.browser)
        # 画像の枠に表示するサイズは画質ごとに異なるため、画質も描画条件に含める
        quality = image_quality(request) if config.image.precompute else None
        return RenderKey(
            target, viewport, request.headers.get("user-agent", ""), quality, mode or page_mode(request)
        )

    def result_response(result: Snapshot | NonHtml, *, embed: bool = False) -> Response:
        nonce = secrets.token_urlsafe(12)
        if isinstance(result, NonHtml):
            size = human(result.size) if result.size is not None else "不明"
            page = templates.non_html_page(result.url, result.content_type, size, nonce)
            return html_response(page, nonce=nonce, embed=embed)
        html, _ = layout.finalize(
            result, nonce, default_quality=default_quality, client_src=CLIENT_SRC, embed=embed
        )
        return html_response(html, nonce=nonce, max_age=300, language=None, embed=embed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await pool.start()
        try:
            yield
        finally:
            await jobs.aclose()
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
    async def home(request: Request) -> HTMLResponse:
        nonce = secrets.token_urlsafe(12)
        # /app のシェルが最初に読み込む iframe の src でもあるため、埋め込み時は frame-ancestors を緩める
        csp = _PWA_CSP_EMBEDDABLE if is_embedded(request) else _PWA_CSP
        return html_response(
            templates.home(nonce, icon_src=ICON_192_SRC),
            nonce=nonce,
            csp=csp.format(nonce=nonce),
        )

    @app.get("/app")
    async def app_shell() -> HTMLResponse:
        """外枠（アドレス欄・戻る/進む）。中身は iframe で /p・/v を表示する。"""
        nonce = secrets.token_urlsafe(12)
        return html_response(
            templates.shell(nonce, icon_src=ICON_192_SRC),
            nonce=nonce,
            csp=_PWA_CSP.format(nonce=nonce),
        )

    @app.get("/static/client.js")
    async def client_js() -> Response:
        return Response(
            _CLIENT_JS,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/static/icon-192.png")
    async def icon_192() -> Response:
        return Response(
            _ICON_192,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/static/icon-512.png")
    async def icon_512() -> Response:
        return Response(
            _ICON_512,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/manifest.json")
    async def manifest() -> Response:
        return Response(
            _MANIFEST,
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/service-worker.js")
    async def service_worker() -> Response:
        return Response(
            _SERVICE_WORKER,
            media_type="text/javascript; charset=utf-8",
            # immutable にしない: ブラウザがバイト単位で内容を比較して更新を検知する仕組みを阻害しないため
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/i")
    async def image(request: Request, u: str = "", q: str = "", r: str = "") -> Response:
        """画像の枠がタップされたときに、選ばれた画質の画像を返す。"""
        if not await guard.allowed(u):
            return PlainTextResponse("Forbidden", status_code=403)
        quality = q if q in QUALITY_LABELS else image_quality(request)
        viewport = viewport_from_cookie(request.cookies.get("lp_env"), config.browser)
        referer = r if r.startswith(("http://", "https://")) else None
        data = await images.variant(
            u,
            quality,
            round(viewport.width * viewport.dpr),
            referer=referer,
            user_agent=request.headers.get("user-agent"),
        )
        if data is None:
            return PlainTextResponse("Not Found", status_code=404)
        return Response(
            data.body,
            media_type=data.content_type,
            headers={
                "Cache-Control": "private, max-age=86400",
                "Content-Security-Policy": _IMAGE_CSP,
                "X-Content-Type-Options": "nosniff",
            },
        )

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
    async def proxy(request: Request, u: str = "", f: str = "", m: str = "") -> Response:
        """m で表示モードを指定できる（layout / reader）。指定すると次のページからも同じモードになる。

        f=1 なら保持している結果を使わずに描画し直す（PC 側のページが失われた場合など）。
        """
        nonce = secrets.token_urlsafe(12)
        embed = is_embedded(request)

        def keep_mode(response: Response) -> Response:
            if m in MODES:
                response.set_cookie("lp_m", m, max_age=31536000, path="/", samesite="lax")
            return response
        target = normalize_input(u)
        if target is None:
            if u.strip():
                return RedirectResponse(f"/s?q={quote_plus(u.strip())}", status_code=303)
            return RedirectResponse("/", status_code=303)
        target = unwrap_redirector(target)
        if not await guard.allowed(target):
            message = "このURLは開けません（内部ネットワーク宛て、またはブロック対象のドメインです）。"
            return html_response(templates.error_page(message, nonce), nonce=nonce, status=403, embed=embed)

        key = render_key(request, target, page_mode(request, m))
        force = f == "1"
        cached = None if force else jobs.cached(key)
        if cached is not None:
            return keep_mode(result_response(cached, embed=embed))
        # 描画を待たずに読み込み中画面を返し、進捗を流し続ける。完了したら /v へ移動させる。
        # no-transform は Cloudflare に圧縮・加工させず、進捗をため込まずに届けるため
        job = jobs.start(key, force=force)
        return keep_mode(
            StreamingResponse(
                loader_stream(job, target, nonce),
                media_type="text/html",
                headers=page_headers(nonce=nonce, cache_control="no-store, no-transform", embed=embed),
            )
        )

    @app.get("/v")
    async def view(request: Request, u: str = "", j: str = "") -> Response:
        """描画結果を表示する。結果が残っていなければ /p からやり直す。"""
        target = normalize_input(u)
        if target is None:
            return RedirectResponse("/", status_code=303)
        job = jobs.get(j)
        result = job.result if job is not None and job.key.url == target else None
        if result is None:
            result = jobs.cached(render_key(request, target))
        if result is None:
            return RedirectResponse(proxy_url(target), status_code=303)
        return result_response(result, embed=is_embedded(request))

    @app.post("/a")
    async def act(request: Request) -> Response:
        """スマホでタップされた JS の UI を PC 側で操作し、画面の差分を返す。"""
        # 独自ヘッダーを必須にし、他のサイトからのフォーム送信などで操作されないようにする
        if request.headers.get("x-lp") != "1":
            return PlainTextResponse("Forbidden", status_code=403)
        def path_ok(value: object) -> bool:
            return (
                isinstance(value, list)
                and len(value) <= 256
                and all(isinstance(i, int) and 0 <= i < 100_000 for i in value)
            )

        try:
            body = await request.json()
            session_id, rev, path = body["s"], body["r"], body["p"]
            kind = body.get("k", "click")
            fields = body.get("v") or []
            submitter = body.get("b")
            valid = (
                isinstance(session_id, str)
                and len(session_id) <= 64
                and isinstance(rev, int)
                and path_ok(path)
                and kind in ("click", "submit")
                and isinstance(fields, list)
                and len(fields) <= 500
                and all(isinstance(f, list) and len(f) == 2 and all(isinstance(x, str) for x in f) for f in fields)
                and sum(len(a) + len(b) for a, b in fields) <= 200_000
                and (submitter is None or path_ok(submitter))
            )
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            return JSONResponse({"error": "要求の形式が不正です。"}, status_code=400)

        quality = image_quality(request) if config.image.precompute else None
        result = await pool.act(
            session_id, rev, path, image_quality=quality, kind=kind, fields=fields, submitter=submitter
        )
        if result.status in ("expired", "reload"):
            return JSONResponse({result.status: True})
        if result.status == "error":
            return JSONResponse({"error": result.message})
        if result.status == "popup" and result.url:
            return JSONResponse({"nav": proxy_url(result.url)})
        if result.status == "navigated" and result.snapshot is not None:
            snap = result.snapshot
            job = jobs.put(render_key(request, snap.url), snap)
            return JSONResponse({"nav": view_url(snap.url, job.id)})
        return JSONResponse({"r": result.rev, "ops": result.ops, "css": result.css})

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
            return html_response(
                templates.error_page("フォームの送信先が不正です。", nonce),
                nonce=nonce,
                status=400,
                embed=is_embedded(request),
            )
        return RedirectResponse(proxy_url(build_form_target(action, fields, charset)), status_code=303)

    @app.post("/f")
    async def form_post(request: Request) -> Response:
        """POST の送信は通常、スマホ側の JS が PC 側のページのフォームへ中継する。
        ここへ届くのは、PC 側のページが保持されていない場合（JS が無効な場合を含む）。"""
        nonce = secrets.token_urlsafe(12)
        action = ""
        with contextlib.suppress(Exception):
            action = (await request.form()).get(FORM_ACTION_FIELD, "")
        message = "PC側のページが保持されていないため、この送信はできません。ページを開き直してから送信してください。"
        target = normalize_input(action) if isinstance(action, str) else None
        return html_response(
            templates.error_page(message, nonce, target=target), nonce=nonce, status=409, embed=is_embedded(request)
        )

    return app
