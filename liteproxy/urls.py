"""中継用 URL の組み立てと、利用者の入力・フォーム送信の解釈。"""

from __future__ import annotations

import codecs
import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

PROXY_PATH = "/p?u="
FORM_PATH = "/f"

# フォーム送信時にサーバー側で使う隠しフィールド（transform.js が埋め込む）
FORM_ACTION_FIELD = "__lp_action"
FORM_CHARSET_FIELD = "__lp_charset"

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_HOST_PORT = re.compile(r"^[^/:]+:\d+(?:[/?#]|$)")

# WHATWG の Shift_JIS は Windows-31J 相当のため、Python の shift_jis では足りない文字がある
_CHARSET_ALIASES = {"shift_jis": "cp932", "windows-31j": "cp932"}


def proxy_url(target: str) -> str:
    return PROXY_PATH + quote(target, safe="")


def normalize_input(raw: str) -> str | None:
    """アドレス欄の入力を URL として解釈する。URL でなければ None（検索語として扱う）。"""
    s = raw.strip()
    if not s:
        return None
    if re.match(r"^https?://", s, re.I):
        candidate = s
    elif _SCHEME.match(s) and not _HOST_PORT.match(s):
        return None  # javascript: や file: などは扱わない
    elif " " not in s and "." in s:
        candidate = "https://" + s
    else:
        return None
    try:
        if not urlsplit(candidate).hostname:
            return None
    except ValueError:
        return None
    return candidate


def unwrap_redirector(url: str) -> str:
    """検索結果のリダイレクタを外し、遷移先の URL を直接返す。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    if (host == "duckduckgo.com" or host.endswith(".duckduckgo.com")) and parts.path == "/l/":
        target = parse_qs(parts.query).get("uddg", [""])[0]
        if target.startswith(("http://", "https://")):
            return target
    return url


def build_form_target(action: str, fields: list[tuple[str, str]], charset: str) -> str:
    """GET フォームの送信先 URL を作る。HTML の仕様どおり action のクエリは置き換える。"""
    parts = urlsplit(action)
    encoding = _CHARSET_ALIASES.get(charset.lower(), charset) or "utf-8"
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = "utf-8"
    query = urlencode(fields, encoding=encoding, errors="xmlcharrefreplace")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
