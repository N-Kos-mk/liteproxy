"""`python -m liteproxy` で起動する。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from .config import ConfigError, load_config
from .main import create_app

DEFAULT_CONFIG = Path("config.toml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="liteproxy", description="liteproxy サーバーを起動する")
    parser.add_argument("-c", "--config", type=Path, help=f"設定ファイル（既定: {DEFAULT_CONFIG}）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("liteproxy")

    path: Path | None = args.config
    if path is None and DEFAULT_CONFIG.exists():
        path = DEFAULT_CONFIG
    try:
        config = load_config(path)
    except ConfigError as e:
        log.error("%s", e)
        return 1
    if path is None:
        log.warning("設定ファイルがないため既定値で起動します")
    if not config.access.enabled:
        log.warning("Access JWT の検証が無効です。公開する場合は [access] を設定してください")

    # reload / workers を使うと Windows では SelectorEventLoop になり、Playwright がブラウザを起動できない
    uvicorn.run(create_app(config), host=config.server.host, port=config.server.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
