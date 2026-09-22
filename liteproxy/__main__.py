"""`python -m liteproxy` で起動する。"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from .config import ConfigError, load_config
from .main import create_app

DEFAULT_CONFIG = Path("config.toml")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="liteproxy", description="liteproxy サーバーを起動する")
    parser.add_argument("-c", "--config", type=Path, help=f"設定ファイル（既定: {DEFAULT_CONFIG}）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    log = logging.getLogger("liteproxy")

    path: Path | None = args.config
    if path is None and DEFAULT_CONFIG.exists():
        path = DEFAULT_CONFIG
    try:
        config = load_config(path)
    except ConfigError as e:
        log.error("%s", e)
        return 1
    # ウィンドウなしで常駐させるとコンソールの出力は見えないため、ファイルにも残す
    if config.log.file:
        log_file = Path(config.log.file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(handler)
    if path is None:
        log.warning("設定ファイルがないため既定値で起動します")
    if not config.access.enabled:
        log.warning("Access JWT の検証が無効です。公開する場合は [access] を設定してください")

    # reload / workers を使うと Windows では SelectorEventLoop になり、Playwright がブラウザを起動できない。
    # log_config=None で uvicorn のログもルートロガー（コンソールとファイル）へ流す
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_level="info",
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
