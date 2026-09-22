"""liteproxy 自身が返す小さなページ（ホーム・エラー・ツールバー）。"""

from __future__ import annotations

from html import escape
from urllib.parse import quote

# スマホの画面情報を Cookie に保存し、次の描画から同じ条件で PC 側に描画させる。
# 表示中のページの viewport 設定に左右されないよう、screen の値を縦向き基準で使う。
_ENV_SCRIPT = (
    "document.cookie='lp_env='+Math.min(screen.width,screen.height)+'_'"
    "+Math.max(screen.width,screen.height)+'_'+devicePixelRatio+'_'"
    "+(matchMedia('(prefers-color-scheme: dark)').matches?1:0)"
    "+';path=/;max-age=31536000;samesite=lax'"
)

_BASE_CSS = (
    ":root{color-scheme:light dark}"
    "body{margin:0;padding:16px;font:16px/1.6 system-ui,sans-serif;max-width:640px}"
    "h1{font-size:18px;margin:0 0 12px}"
    "form{display:flex;gap:8px}"
    "input{flex:1;min-width:0;font:inherit;padding:8px}"
    "button{font:inherit;padding:8px 14px}"
    "p{margin:12px 0}"
)

BAR_CSS = (
    "lp-bar{all:initial;display:flex;gap:10px;align-items:center;padding:3px 8px;"
    "background:#1f2328;color:#d0d7de;font:12px/1.7 system-ui,sans-serif;white-space:nowrap;overflow:hidden}"
    "lp-bar *{all:unset}"
    "lp-bar a{color:#79c0ff;cursor:pointer}"
    "lp-bar b{font-weight:700;color:#fff}"
    "lp-bar .t{flex:1;overflow:hidden;text-overflow:ellipsis}"
)


def env_script(nonce: str) -> str:
    return f'<script nonce="{nonce}">{_ENV_SCRIPT}</script>'


def _page(title: str, body: str, nonce: str) -> str:
    return (
        '<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_BASE_CSS}</style></head>"
        f"<body>{body}{env_script(nonce)}</body></html>"
    )


def _address_form(value: str = "") -> str:
    return (
        '<form action="/p"><input name="u" type="search" placeholder="URL または検索語" '
        f'value="{escape(value)}" autocapitalize="off" autocomplete="off" enterkeyhint="go">'
        "<button>開く</button></form>"
    )


def home(nonce: str) -> str:
    return _page("liteproxy", "<h1>liteproxy</h1>" + _address_form(), nonce)


def error_page(message: str, nonce: str, *, target: str | None = None) -> str:
    body = f"<h1>開けませんでした</h1><p>{escape(message)}</p>"
    if target:
        body += (
            f'<p><a href="/p?u={quote(target, safe="")}">再試行</a> / '
            f'<a href="{escape(target)}" rel="noreferrer">元のページを直接開く（通信量に注意）</a></p>'
        )
    body += _address_form(target or "")
    return _page("エラー - liteproxy", body, nonce)


def non_html_page(url: str, content_type: str, size_text: str, nonce: str) -> str:
    body = (
        "<h1>HTML ではないファイルです</h1>"
        f"<p>種類: {escape(content_type)}<br>サイズ: {escape(size_text)}</p>"
        f'<p><a href="{escape(url)}" rel="noreferrer">直接開く（通信量に注意）</a></p>'
    ) + _address_form(url)
    return _page("ファイル - liteproxy", body, nonce)


def toolbar(*, title: str, url: str, sent: str, fetched: str, nonce: str) -> str:
    return (
        f'<lp-bar><a href="/"><b>LP</b></a><span class="t">{escape(title)}</span>'
        f'<span title="送信量（圧縮後の目安） / PC 側の取得量">{escape(sent)} / {escape(fetched)}</span>'
        f'<a href="{escape(url)}" rel="noreferrer" title="元のページを直接開く（通信量に注意）">元</a>'
        f"</lp-bar>{env_script(nonce)}"
    )
