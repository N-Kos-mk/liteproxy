from urllib.parse import quote

import jwt
import pytest
from fastapi.testclient import TestClient

from liteproxy.browser import NonHtml, RenderError, Snapshot, Viewport
from liteproxy.config import Config
from liteproxy.main import create_app, viewport_from_cookie

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
    def __init__(self, result=SNAPSHOT):
        self.result = result
        self.calls = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def render(self, url, viewport, user_agent):
        self.calls.append((url, viewport, user_agent))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_client(renderer=None, **kwargs):
    config = Config()
    config.log.stats_file = ""
    app = create_app(config, renderer=renderer or FakeRenderer(), **kwargs)
    return TestClient(app, follow_redirects=False)


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


def test_proxy_renders_with_phone_viewport():
    renderer = FakeRenderer()
    with make_client(renderer) as client:
        client.cookies.set("lp_env", "412_915_2.625_1")
        res = client.get("/p", params={"u": "https://8.8.8.8/"}, headers={"User-Agent": "PhoneUA"})
    assert res.status_code == 200
    assert renderer.calls == [("https://8.8.8.8/", Viewport(412, 915, 2.625, True), "PhoneUA")]
    assert "<lp-bar>" in res.text and "<!--lp-bar-->" not in res.text
    assert "タイトル" in res.text
    assert "1.9MB" in res.text  # PC 側の取得量
    assert "private, max-age=300" == res.headers["cache-control"]
    assert "content-language" not in res.headers  # 中継したページは元の言語に任せる
    nonce = res.headers["content-security-policy"].split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in res.text


def test_large_page_is_gzipped():
    snap = Snapshot(**{**SNAPSHOT.__dict__, "html": SNAPSHOT.html.replace("本文", "本文" * 2000)})
    with make_client(FakeRenderer(snap)) as client:
        res = client.get("/p", params={"u": "https://8.8.8.8/"}, headers={"Accept-Encoding": "gzip"})
    assert res.headers["content-encoding"] == "gzip"


def test_render_error_page():
    with make_client(FakeRenderer(RenderError("タイムアウト"))) as client:
        res = client.get("/p", params={"u": "https://8.8.8.8/"})
    assert res.status_code == 502
    assert "タイムアウト" in res.text and "再試行" in res.text


def test_non_html_page():
    with make_client(FakeRenderer(NonHtml("https://8.8.8.8/a.pdf", "application/pdf", 2048))) as client:
        res = client.get("/p", params={"u": "https://8.8.8.8/a.pdf"})
    assert res.status_code == 200
    assert "application/pdf" in res.text and "2.0KB" in res.text


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
