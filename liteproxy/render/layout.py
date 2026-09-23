"""layout モード: 元のレイアウトを保ったまま、画像・JS・フォントを除いたページを返す。"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..browser import Snapshot
from ..stats import PageStats, compressed_size, human
from ..templates import BAR_CSS, toolbar


def finalize(
    snap: Snapshot, nonce: str, *, default_quality: str, client_src: str, embed: bool = False
) -> tuple[str, PageStats]:
    """変換済みスナップショット（layout / reader）にツールバーを差し込み、送信する HTML と統計を返す。

    embed が真のときは /app のシェルが iframe に埋め込んでいるため、ツールバーを隠して差し込む。
    """
    gzip_bytes = compressed_size(snap.html)
    bar = toolbar(
        title=snap.title or urlsplit(snap.url).hostname or snap.url,
        url=snap.url,
        sent=human(gzip_bytes),
        fetched=human(snap.pc_bytes),
        nonce=nonce,
        sized_quality=snap.image_quality,
        default_quality=default_quality,
        client_src=client_src,
        session_id=snap.session_id,
        rev=snap.rev,
        mode=snap.mode,
        embed=embed,
    )
    # liteproxy が足す要素には data-lp-x を付け、操作の差分で要素の位置を数えるときに除く
    html = snap.html.replace("<!--lp-bar-->", bar, 1).replace(
        "</head>", f"<style data-lp-x>{BAR_CSS}</style></head>", 1
    )
    stats = PageStats(
        url=snap.url,
        elapsed_ms=snap.elapsed_ms,
        transform_ms=snap.transform_ms,
        image_ms=snap.image_ms,
        image_count=snap.image_count,
        pc_bytes=snap.pc_bytes,
        requests=snap.requests,
        blocked=snap.blocked,
        html_bytes=len(html.encode("utf-8")),
        gzip_bytes=gzip_bytes,
        css_bytes=snap.css_bytes,
    )
    return html, stats
