import pytest

from liteproxy.urls import build_form_target, normalize_input, proxy_url, unwrap_redirector


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com/a", "https://example.com/a"),
        ("  http://example.com  ", "http://example.com"),
        ("HTTPS://Example.com/x?y=1", "HTTPS://Example.com/x?y=1"),
        ("example.com:8080/x", "https://example.com:8080/x"),
        ("天気 東京", None),
        ("localhost:8000", None),
        ("javascript:alert(1)", None),
        ("file:///C:/Windows/win.ini", None),
        ("mailto:a@example.com", None),
        ("", None),
    ],
)
def test_normalize_input(raw, expected):
    assert normalize_input(raw) == expected


def test_proxy_url_encodes_everything():
    assert proxy_url("https://e.com/?a=1&b=2") == "/p?u=https%3A%2F%2Fe.com%2F%3Fa%3D1%26b%3D2"


def test_unwrap_duckduckgo_redirect():
    url = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert unwrap_redirector(url) == "https://example.com/page"


def test_unwrap_ignores_non_http_target():
    url = "https://duckduckgo.com/l/?uddg=javascript%3Aalert(1)"
    assert unwrap_redirector(url) == url


def test_unwrap_leaves_other_urls():
    assert unwrap_redirector("https://example.com/l/?uddg=x") == "https://example.com/l/?uddg=x"


def test_form_target_replaces_query_and_keeps_fragment():
    target = build_form_target("https://e.com/s?old=1#top", [("q", "a b"), ("q", "c")], "UTF-8")
    assert target == "https://e.com/s?q=a+b&q=c#top"


def test_form_target_uses_page_charset():
    # Shift_JIS のページのフォームは Shift_JIS でエンコードして送る（日 = 93FA, 本 = 967B）
    assert build_form_target("https://e.com/s", [("q", "日本")], "Shift_JIS") == "https://e.com/s?q=%93%FA%96%7B"


def test_form_target_unknown_charset_falls_back_to_utf8():
    assert build_form_target("https://e.com/s", [("q", "日")], "x-unknown") == "https://e.com/s?q=%E6%97%A5"
