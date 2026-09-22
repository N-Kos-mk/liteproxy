"""転送量の計測と記録。実際にスマホへ送った量を見ながら変換方法を調整するために使う。"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def human(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def compressed_size(text: str) -> int:
    """スマホへの転送量の目安。Cloudflare は brotli を使うため、実際はこれより少し小さい。"""
    return len(gzip.compress(text.encode("utf-8"), compresslevel=6))


@dataclass
class PageStats:
    url: str
    elapsed_ms: int
    transform_ms: int
    pc_bytes: int
    requests: int
    blocked: int
    html_bytes: int
    gzip_bytes: int
    css_bytes: int


class StatsLog:
    """1 ページ 1 行の JSON Lines として追記する。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path) if path else None

    def write(self, stats: PageStats) -> None:
        log.info(
            "%s %dms(変換 %dms) PC取得=%s 送信=%s(gzip %s) CSS=%s",
            stats.url,
            stats.elapsed_ms,
            stats.transform_ms,
            human(stats.pc_bytes),
            human(stats.html_bytes),
            human(stats.gzip_bytes),
            human(stats.css_bytes),
        )
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"time": int(time.time()), **asdict(stats)}, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("統計を書き込めません: %s", e)
