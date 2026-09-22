"""layout モード: 元のレイアウトを保ったまま、画像・JS・フォントを除いたページを返す。"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..browser import Snapshot
from ..stats import PageStats, compressed_size, human
from ..templates import BAR_CSS, toolbar


def finalize(snap: Snapshot, nonce: str) -> tuple[str, PageStats]:
    """変換済みスナップショットにツールバーを差し込み、送信する HTML と統計を返す。"""
    gzip_bytes = compressed_size(snap.html)
    bar = toolbar(
        title=snap.title or urlsplit(snap.url).hostname or snap.url,
        url=snap.url,
        sent=human(gzip_bytes),
        fetched=human(snap.pc_bytes),
        nonce=nonce,
    )
    html = snap.html.replace("<!--lp-bar-->", bar, 1).replace("</head>", f"<style>{BAR_CSS}</style></head>", 1)
    stats = PageStats(
        url=snap.url,
        elapsed_ms=snap.elapsed_ms,
        transform_ms=snap.transform_ms,
        pc_bytes=snap.pc_bytes,
        requests=snap.requests,
        blocked=snap.blocked,
        html_bytes=len(html.encode("utf-8")),
        gzip_bytes=gzip_bytes,
        css_bytes=snap.css_bytes,
    )
    return html, stats
