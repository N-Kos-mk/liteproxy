import asyncio
import http.server
import io
import json
import re
import threading
from urllib.parse import quote

import jwt
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from liteproxy.browser import ActResult, NonHtml, RenderError, Snapshot, Viewport
from liteproxy.config import Config
from liteproxy.main import CLIENT_SRC, create_app, viewport_from_cookie

SNAPSHOT = Snapshot(
    url="https://8.8.8.8/final",
    title="タイトル",
    html="<!DOCTYPE html><html><head><style></style></head><body><!--lp-bar--><p>本文</p></body></html>",
    pc_bytes=2_000_000,
    requests=50,
    blocked=3,
    css_bytes=10,
    elapsed_ms=1234,
    transform_ms=100,
)


class FakeRenderer:
    def __init__(self, result=SNAPSHOT, *, delay=0.0, act_result=None):
        self.result = result
        self.delay = delay
        self.calls = []
        self.act_result = act_result or ActResult("expired")
        self.act_calls = []

    async def act(self, session_id, rev, path, image_quality=None):
        self.act_calls.append((session_id, rev, path))
        return self.act_result

    async def start(self):
        pass

    async def stop(self):
        pass

    async def render(self, url, viewport, user_agent, on_stage=None, image_quality=None):
        self.calls.append((url, viewport, user_agent))
        self.image_quality = image_quality
        for stage in ("fetching", "running", "transforming"):
            if on_stage:
                on_stage(stage)
            await asyncio.sleep(self.delay)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_client(renderer=None, config=None, **kwargs):
    config = config or Config()
    config.log.stats_file = ""
    app = create_app(config, renderer=renderer or FakeRenderer(), **kwargs)
    return TestClient(app, follow_redirects=False)


def open_page(client, url, **kwargs):
    """/p の読み込み中画面を最後まで受け取り、移動先の /v を開く。"""
    loader = client.get("/p", params={"u": url}, **kwargs)
    m = re.search(r'lp\("done", \d+, (\{.*?\})\)</script>', loader.text)
    assert m, "読み込み中画面が完了を通知していない"
    return loader, client.get(json.loads(m.group(1))["next"], **kwargs)


def test_home_has_address_form_and_csp():
    with make_client() as client:
        res = client.get("/")
    assert res.status_code == 200
    assert 'action="/p"' in res.text
    assert "default-src 'none'" in res.headers["content-security-policy"]
    assert res.headers["content-language"] == "ja"
    assert '<html lang="ja">' in res.text


def test_non_url_input_goes_to_search():
    with make_client() as client:
        res = client.get("/p", params={"u": "天気 東京"})
    assert res.status_code == 303
    assert res.headers["location"] == "/s?q=%E5%A4%A9%E6%B0%97+%E6%9D%B1%E4%BA%AC"


def test_search_redirects_to_duckduckgo():
    with make_client() as client:
        res = client.get("/s", params={"q": "a b"})
    assert res.headers["location"] == "/p?u=" + quote("https://html.duckduckgo.com/html/?q=a+b", safe="")


def test_private_url_is_rejected():
    renderer = FakeRenderer()
    with make_client(renderer) as client:
        res = client.get("/p", params={"u": "http://192.168.1.1/"})
    assert res.status_code == 403
    assert renderer.calls == []


def test_loader_then_result_with_phone_viewport():
    renderer = FakeRenderer()
    with make_client(renderer) as client:
        client.cookies.set("lp_env", "412_915_2.625_1")
        loader, res = open_page(client, "https://8.8.8.8/", headers={"User-Agent": "PhoneUA"})
    # 読み込み中画面は加工・キャッシュさせない
    assert loader.status_code == 200
    assert loader.headers["cache-control"] == "no-store, no-transform"
    assert "PCで処理しています" in loader.text
    assert renderer.calls == [("https://8.8.8.8/", Viewport(412, 915, 2.625, True), "PhoneUA")]
    # 移動先の /v に描画結果が出る
    assert res.status_code == 200
    assert "<lp-bar " in res.text and "<!--lp-bar-->" not in res.text
    assert "タイトル" in res.text
    assert "1.9MB" in res.text  # PC 側の取得量
    assert res.headers["cache-control"] == "private, max-age=300"
    assert "content-language" not in res.headers  # 中継したページは元の言語に任せる
    nonce = res.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}" data-lp-x>' in res.text


def test_loader_reports_each_stage():
    with make_client(FakeRenderer(delay=0.05)) as client:
        loader, _ = open_page(client, "https://8.8.8.8/")
    for stage in ("fetching", "running", "transforming", "done"):
        assert f'lp("{stage}"' in loader.text


def test_recent_result_is_served_without_loader():
    renderer = FakeRenderer()
    with make_client(renderer) as client:
        open_page(client, "https://8.8.8.8/")
        res = client.get("/p", params={"u": "https://8.8.8.8/"})
    assert "<lp-bar " in res.text and "PCで処理しています" not in res.text
    assert len(renderer.calls) == 1


def test_view_without_result_starts_over():
    with make_client() as client:
        res = client.get("/v", params={"u": "https://8.8.8.8/", "j": "unknown"})
    assert res.status_code == 303
    assert res.headers["location"] == "/p?u=" + quote("https://8.8.8.8/", safe="")


def test_large_page_is_gzipped():
    snap = Snapshot(**{**SNAPSHOT.__dict__, "html": SNAPSHOT.html.replace("本文", "本文" * 2000)})
    with make_client(FakeRenderer(snap)) as client:
        _, res = open_page(client, "https://8.8.8.8/", headers={"Accept-Encoding": "gzip"})
    assert res.headers["content-encoding"] == "gzip"


def test_render_error_is_shown_on_loader():
    with make_client(FakeRenderer(RenderError("タイムアウト"))) as client:
        res = client.get("/p", params={"u": "https://8.8.8.8/"})
    assert res.status_code == 200
    assert 'lp("error"' in res.text and "タイムアウト" in res.text and "再試行" in res.text


def test_non_html_page():
    with make_client(FakeRenderer(NonHtml("https://8.8.8.8/a.pdf", "application/pdf", 2048))) as client:
        _, res = open_page(client, "https://8.8.8.8/a.pdf")
    assert res.status_code == 200
    assert "application/pdf" in res.text and "2.0KB" in res.text


def test_toolbar_passes_image_settings_to_client():
    with make_client() as client:
        client.cookies.set("lp_q", "low")
        _, res = open_page(client, "https://8.8.8.8/")
        js = client.get(CLIENT_SRC)
    assert 'data-page="https://8.8.8.8/final"' in res.text
    assert 'data-dq="mid"' in res.text and 'id="lp-img"' in res.text
    assert f'<script src="{CLIENT_SRC}" defer data-lp-x></script>' in res.text
    assert "img-src 'self' data:" in res.headers["content-security-policy"]
    assert js.headers["content-type"].startswith("text/javascript")
    assert "immutable" in js.headers["cache-control"]


@pytest.mark.parametrize(("cookie", "precompute", "expected"), [(None, True, "mid"), ("low", True, "low"),
                                                                ("bogus", True, "mid"), ("low", False, None)])
def test_image_quality_for_render(cookie, precompute, expected):
    config = Config()
    config.image.precompute = precompute
    renderer = FakeRenderer()
    with make_client(renderer, config) as client:
        if cookie:
            client.cookies.set("lp_q", cookie)
        open_page(client, "https://8.8.8.8/")
    assert renderer.image_quality == expected


class _ImageHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        ctype, body = self.routes.get(self.path, ("text/plain", b""))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture(scope="module")
def image_server():
    out = io.BytesIO()
    Image.new("RGB", (1200, 600), (200, 30, 30)).save(out, "PNG")
    _ImageHandler.routes = {"/a.png": ("image/png", out.getvalue()), "/page.html": ("text/html", b"<p>x</p>")}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_image_endpoint_serves_converted_image(image_server):
    config = Config()
    config.network.allow_private = True
    with make_client(config=config) as client:
        res = client.get("/i", params={"u": image_server + "/a.png", "q": "low"})
        page = client.get("/i", params={"u": image_server + "/page.html", "q": "low"})
    assert res.status_code == 200 and res.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(res.content)).size == (320, 160)
    assert "sandbox" in res.headers["content-security-policy"]
    assert res.headers["cache-control"] == "private, max-age=86400"
    assert page.status_code == 404  # 画像以外は返さない


def test_image_endpoint_rejects_private_address(image_server):
    with make_client() as client:
        assert client.get("/i", params={"u": image_server + "/a.png"}).status_code == 403


def test_force_rerenders_even_with_recent_result():
    renderer = FakeRenderer()
    with make_client(renderer) as client:
        open_page(client, "https://8.8.8.8/")
        res = client.get("/p", params={"u": "https://8.8.8.8/", "f": "1"})
    assert "PCで処理しています" in res.text
    assert len(renderer.calls) == 2


ACT = {"s": "sess", "r": 3, "p": [1, 0, 2]}
LP = {"X-LP": "1"}


def test_act_requires_custom_header():
    renderer = FakeRenderer()
    with make_client(renderer) as client:
        res = client.post("/a", json=ACT)
    assert res.status_code == 403 and renderer.act_calls == []


@pytest.mark.parametrize("body", [{}, {"s": "x", "r": "1", "p": []}, {"s": "x", "r": 1, "p": ["a"]},
                                  {"s": "x", "r": 1, "p": [-1]}, {"s": "x" * 100, "r": 1, "p": []}])
def test_act_rejects_malformed_body(body):
    with make_client() as client:
        assert client.post("/a", json=body, headers=LP).status_code == 400


def test_act_returns_diff():
    result = ActResult("ok", rev=4, ops=[{"t": "a", "p": [1], "a": {"class": "x"}}], css=[".x{color:red}"])
    renderer = FakeRenderer(act_result=result)
    with make_client(renderer) as client:
        res = client.post("/a", json=ACT, headers=LP)
    assert res.json() == {"r": 4, "ops": result.ops, "css": result.css}
    assert renderer.act_calls == [("sess", 3, [1, 0, 2])]


@pytest.mark.parametrize("status", ["expired", "reload"])
def test_act_tells_client_to_reload(status):
    with make_client(FakeRenderer(act_result=ActResult(status))) as client:
        assert client.post("/a", json=ACT, headers=LP).json() == {status: True}


def test_act_navigation_is_served_from_view():
    moved = Snapshot(**{**SNAPSHOT.__dict__, "url": "https://8.8.8.8/next", "title": "次のページ"})
    renderer = FakeRenderer(act_result=ActResult("navigated", rev=5, snapshot=moved))
    with make_client(renderer) as client:
        nav = client.post("/a", json=ACT, headers=LP).json()["nav"]
        res = client.get(nav)
    assert nav.startswith("/v?u=" + quote("https://8.8.8.8/next", safe=""))
    assert "次のページ" in res.text and len(renderer.calls) == 0  # 描画し直さずに遷移先を表示する


def test_act_popup_opens_through_proxy():
    renderer = FakeRenderer(act_result=ActResult("popup", url="https://8.8.8.8/tab"))
    with make_client(renderer) as client:
        assert client.post("/a", json=ACT, headers=LP).json() == {"nav": "/p?u=" + quote("https://8.8.8.8/tab", safe="")}


def test_csp_allows_only_same_origin_requests():
    with make_client() as client:
        csp = client.get("/").headers["content-security-policy"]
    assert "connect-src 'self'" in csp


def test_get_form_is_forwarded():
    with make_client() as client:
        res = client.get(
            "/f",
            params=[("q", "x y"), ("__lp_action", "https://e.com/search?old=1"), ("__lp_charset", "UTF-8")],
        )
    assert res.status_code == 303
    assert res.headers["location"] == "/p?u=" + quote("https://e.com/search?q=x+y", safe="")


def test_form_with_bad_action_is_rejected():
    with make_client() as client:
        res = client.get("/f", params={"q": "x", "__lp_action": "javascript:alert(1)"})
    assert res.status_code == 400


def test_post_form_is_not_supported_yet():
    with make_client() as client:
        res = client.post("/f")
    assert res.status_code == 501


class _Verifier:
    def verify(self, token):
        if token != "good":
            raise jwt.InvalidTokenError("bad")
        return {}


@pytest.mark.parametrize(("headers", "status"), [({}, 403), ({"Cf-Access-Jwt-Assertion": "bad"}, 403),
                                                 ({"Cf-Access-Jwt-Assertion": "good"}, 200)])
def test_access_middleware(headers, status):
    with make_client(verifier=_Verifier()) as client:
        assert client.get("/", headers=headers).status_code == status


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, Viewport(390, 844, 3.0, False)),
        ("375_667_2_0", Viewport(375, 667, 2.0, False)),
        ("99999_1_9_1", Viewport(1280, 320, 4.0, True)),  # 範囲外は丸める
        ("broken", Viewport(390, 844, 3.0, False)),
    ],
)
def test_viewport_from_cookie(value, expected):
    assert viewport_from_cookie(value, Config().browser) == expected
