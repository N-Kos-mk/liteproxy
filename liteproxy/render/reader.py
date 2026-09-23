"""reader モード: 本文だけを抽出し、サイトの CSS を使わない最小限の HTML にする。

trafilatura が返す XML（head / p / list / graphic / ref など）を、liteproxy の HTML へ変換する。
画像は layout モードと同じ枠（タップで読み込む）にし、リンクは中継経由に書き換える。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from html import escape

import trafilatura
from lxml.etree import _Element

from ..stats import human
from ..urls import proxy_url

# 画像の枠（img タグ）を作る関数。url と alt を受け取る
PlaceholderFn = Callable[[str, str], str]

log = logging.getLogger(__name__)

READER_CSS = (
    ":root{color-scheme:light dark}"
    "body{margin:0 auto;max-width:40em;padding:0 16px 40px;font:17px/1.8 system-ui,sans-serif}"
    "h1{font-size:1.4em;line-height:1.4;margin:20px 0 4px}"
    "h2{font-size:1.2em;margin:1.8em 0 .4em}"
    "h3{font-size:1.05em;margin:1.5em 0 .4em}"
    "p{margin:1em 0}"
    "img{max-width:100%;height:auto;display:block;margin:0 auto}"
    "figure{margin:1.4em 0}"
    "figcaption{font-size:.85em;color:#888;text-align:center;margin-top:.4em}"
    "blockquote{margin:1.2em 0;padding-left:12px;border-left:3px solid #8886;color:#888}"
    "pre{overflow-x:auto;padding:8px;background:#8881;border-radius:4px}"
    "code{font-family:ui-monospace,monospace;font-size:.9em}"
    "table{border-collapse:collapse;display:block;overflow-x:auto}"
    "th,td{border:1px solid #8886;padding:4px 8px;text-align:left}"
    "a{color:#3b82f6}"
    "hr{border:0;border-top:1px solid #8886;margin:1.6em 0}"
)

# trafilatura の XML タグ → 出力するタグ
_HEADINGS = {"h1": "h1", "h2": "h2", "h3": "h3", "h4": "h4", "h5": "h4", "h6": "h4"}
_BLOCKS = {"p": "p", "quote": "blockquote", "table": "table", "row": "tr", "cell": "td", "code": "pre"}
_INLINE = {"#b": "strong", "#i": "em", "#u": "u", "#t": "code"}


@dataclass
class Article:
    title: str
    body: str  # 変換後の HTML（本文のみ）
    chars: int  # 本文の文字数（抽出できたかの判断に使う）


def extract(html: str, url: str) -> object | None:
    """描画済みの HTML から本文を抽出する。抽出できない場合は None。"""
    try:
        return trafilatura.bare_extraction(
            html,
            url=url,
            include_images=True,
            include_links=True,
            include_formatting=True,
            include_tables=True,
            include_comments=False,
            with_metadata=True,  # 記事のタイトルを得る
        )
    except Exception as e:  # noqa: BLE001  抽出は失敗しても layout モードへ切り替えれば足りる
        log.debug("本文を抽出できません: %s", e)
        return None


def build(
    document: object,
    *,
    placeholder: PlaceholderFn,
    fallback_title: str = "",
) -> Article:
    """抽出結果を HTML に変換する。placeholder は画像の枠を作る関数。"""
    body = getattr(document, "body", None)
    parts: list[str] = []
    if body is not None:
        for child in body:
            _render(child, parts, placeholder)
    text = "".join(parts)
    title = (getattr(document, "title", None) or fallback_title or "").strip()
    chars = len(_strip_tags(text))
    # 本文に見出しが含まれていない場合だけ、タイトルを先頭に置く
    head = f"<h1>{escape(title)}</h1>" if title and "<h1>" not in text else ""
    return Article(title=title, body=head + text, chars=chars)


def page(article: Article, *, lang: str = "ja") -> str:
    """スマホへ返す HTML。ツールバーは layout モードと同じ位置に差し込む。"""
    return (
        f'<!DOCTYPE html><html lang="{escape(lang)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(article.title or '本文')}</title><style>{READER_CSS}</style></head>"
        f"<body><!--lp-bar--><article>{article.body}</article></body></html>"
    )


def placeholders(
    dims: dict[str, tuple[int, int]], sizes: dict[str, int], display_width: int
) -> PlaceholderFn:
    """画像の枠を作る関数を返す。dims は元画像の寸法、sizes は送信サイズ（画質変換後）。"""

    def make(url: str, alt: str) -> str:
        w, h = dims.get(url) or (640, 360)
        w, h = max(1, w), max(1, h)
        size = sizes.get(url)
        shown = min(w, max(80, display_width))
        scale = w / shown
        label = " ".join((alt or "画像").split()) or "画像"
        suffix = human(size) if size is not None else ""
        limit = max(2, int(shown / 12) - 1 - (len(suffix) // 2 + 1 if suffix else 0))
        if len(label) > limit:
            label = label[: max(1, limit - 1)] + "…"
        text = f"{label} {suffix}".strip()
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}'>"
            "<rect width='100%' height='100%' fill='#888' fill-opacity='.2'/>"
            f"<text x='50%' y='50%' text-anchor='middle' dominant-baseline='central' "
            f"font-family='sans-serif' font-size='{max(1, round(11 * scale))}' fill='#666'>"
            f"{escape(text, quote=False)}</text></svg>"
        )
        src = "data:image/svg+xml," + (
            svg.replace("%", "%25").replace("#", "%23").replace("<", "%3C").replace(">", "%3E")
        )
        size_attr = f' data-lp-size="{size}"' if size is not None else ""
        return (
            f'<img src="{src}" width="{w}" height="{h}" alt="{escape(alt)}" '
            f'data-lp-src="{escape(url)}"{size_attr}>'
        )

    return make


def _render(el: _Element, out: list[str], placeholder: PlaceholderFn) -> None:
    tag = el.tag if isinstance(el.tag, str) else ""
    rend = el.get("rend") or ""
    if tag == "graphic":
        src = el.get("src") or ""
        alt = el.get("alt") or el.get("title") or ""
        if src.startswith(("http://", "https://")):
            out.append(placeholder(src, alt))
        _tail(el, out)
        return
    if tag == "ref":
        target = el.get("target") or ""
        href = proxy_url(target) if target.startswith(("http://", "https://")) else ""
        out.append(f'<a href="{escape(href)}">' if href else "<span>")
        _children(el, out, placeholder)
        out.append("</a>" if href else "</span>")
        _tail(el, out)
        return
    if tag == "head":
        name = _HEADINGS.get(rend, "h2")
        out.append(f"<{name}>")
        _children(el, out, placeholder)
        out.append(f"</{name}>")
        _tail(el, out)
        return
    if tag == "list":
        name = "ol" if rend == "ordered" else "ul"
        out.append(f"<{name}>")
        for item in el:
            if isinstance(item.tag, str) and item.tag == "item":
                out.append("<li>")
                _children(item, out, placeholder)
                out.append("</li>")
            else:
                _render(item, out, placeholder)
        out.append(f"</{name}>")
        _tail(el, out)
        return
    if tag == "hi":
        name = _INLINE.get(rend, "em")
        out.append(f"<{name}>")
        _children(el, out, placeholder)
        out.append(f"</{name}>")
        _tail(el, out)
        return
    if tag == "lb":
        out.append("<br>")
        _tail(el, out)
        return
    if tag in _BLOCKS:
        name = _BLOCKS[tag]
        out.append(f"<{name}>")
        _children(el, out, placeholder)
        out.append(f"</{name}>")
        _tail(el, out)
        return
    # 未知のタグは中身だけを残す
    _children(el, out, placeholder)
    _tail(el, out)


def _children(el: _Element, out: list[str], placeholder: PlaceholderFn) -> None:
    if el.text:
        out.append(escape(el.text))
    for child in el:
        _render(child, out, placeholder)


def _tail(el: _Element, out: list[str]) -> None:
    if el.tail:
        out.append(escape(el.tail))


def _strip_tags(html: str) -> str:
    depth = 0
    text = []
    for ch in html:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            text.append(ch)
    return "".join(text).strip()
