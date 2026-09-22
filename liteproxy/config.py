"""TOML 設定ファイルの読み込み。

各セクションは dataclass で表し、ファイルに書かれていない項目は既定値を使う。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """設定ファイルの内容が不正。"""


@dataclass
class ServerConfig:
    # cloudflared からのみ到達させるため、既定ではループバックだけで待ち受ける
    host: str = "127.0.0.1"
    port: int = 8700


@dataclass
class AccessConfig:
    # Cloudflare Access が付与する JWT をアプリ側でも検証する（多層防御）
    enabled: bool = False
    team_domain: str = ""  # 例: yourteam.cloudflareaccess.com
    aud: str = ""  # Access アプリケーションの Audience (AUD) タグ
    allowed_emails: list[str] = field(default_factory=list)  # 空なら Access のポリシーに任せる


@dataclass
class BrowserConfig:
    channel: str = "chrome"  # 空文字なら Playwright 同梱の Chromium
    max_concurrency: int = 2
    nav_timeout_ms: int = 20000  # HTML の読み込み（DOMContentLoaded）までの上限
    settle_timeout_ms: int = 5000  # その後、JS・CSS・XHR の通信が落ち着くまで待つ上限
    quiet_ms: int = 400  # この時間通信が途切れたら落ち着いたとみなす
    scroll_max_steps: int = 15
    scroll_delay_ms: int = 120
    image_wait_ms: int = 1500  # スクロール後、寸法の分からない画像を待つ上限
    # スマホから画面情報の Cookie が届く前に使う既定値
    default_width: int = 390
    default_height: int = 844
    default_dpr: float = 3.0


@dataclass
class RenderConfig:
    max_inline_svg: int = 3000  # これより大きいインライン SVG は中身を捨てて枠だけ残す
    max_data_uri: int = 2048  # これ以下の data: URI（小さなアイコン等）はそのまま残す
    prune_classes: bool = True  # CSS から参照されない class 名を削る
    # 描画結果から取り除く要素（主に Cookie 同意バナー。JS なしでは閉じられないため）
    remove_selectors: list[str] = field(
        default_factory=lambda: [
            "#onetrust-consent-sdk",
            "#CybotCookiebotDialog",
            "#didomi-host",
            ".fc-consent-root",
            "#usercentrics-root",
            ".qc-cmp2-container",
        ]
    )


@dataclass
class SessionConfig:
    # 描画したページを PC 側で保持し、JS で動く UI（開閉・タブ・メニューなど）の操作を中継する
    enabled: bool = True
    max_sessions: int = 3  # 同時に保持するページの数（1 ページで 100〜300MB 程度のメモリを使う）
    idle_minutes: int = 10  # 操作がないまま経過したら破棄するまでの時間
    settle_ms: int = 3000  # 操作後、ページの変化が落ち着くまで待つ上限
    quiet_ms: int = 250  # この時間 DOM の変化が途切れたら落ち着いたとみなす
    tap_timeout_ms: int = 2000  # 要素がタップできる状態になるまで待つ上限


@dataclass
class ImageConfig:
    default_quality: str = "mid"  # 画質を選んでいないときの既定（low / mid / high / orig）
    precompute: bool = True  # 描画時に既定の画質へ変換し、画像の枠にサイズを表示する
    cache_mb: int = 256  # 元画像と変換結果を保持するメモリの上限
    max_image_mb: int = 20  # 1 枚あたりの取得上限


@dataclass
class NetworkConfig:
    # true にすると LAN・ループバック宛ての取得を許可する（テスト用。通常は false）
    allow_private: bool = False
    # PC 側での取得自体を止めるドメイン（サブドメインを含む）。広告・同意管理が中心
    block_domains: list[str] = field(
        default_factory=lambda: [
            "doubleclick.net",
            "googlesyndication.com",
            "googleadservices.com",
            "googletagservices.com",
            "adservice.google.com",
            "amazon-adsystem.com",
            "criteo.com",
            "criteo.net",
            "taboola.com",
            "outbrain.com",
            "adnxs.com",
            "rubiconproject.com",
            "pubmatic.com",
            "scorecardresearch.com",
            "i-mobile.co.jp",
            "microad.jp",
            "cdn.cookielaw.org",
            "consent.cookiebot.com",
        ]
    )


@dataclass
class SearchConfig:
    # {q} に URL エンコード済みの検索語が入る
    url: str = "https://html.duckduckgo.com/html/?q={q}"


@dataclass
class LogConfig:
    file: str = "logs/liteproxy.log"  # 動作ログ（1MB ごとに切り替え、3 世代まで保持）。空文字なら出力しない
    stats_file: str = "logs/stats.jsonl"  # 空文字なら記録しない


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    access: AccessConfig = field(default_factory=AccessConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    log: LogConfig = field(default_factory=LogConfig)


_SECTIONS = {
    "server": ServerConfig,
    "access": AccessConfig,
    "browser": BrowserConfig,
    "render": RenderConfig,
    "session": SessionConfig,
    "image": ImageConfig,
    "network": NetworkConfig,
    "search": SearchConfig,
    "log": LogConfig,
}


def load_config(path: Path | None) -> Config:
    """設定ファイルを読み込む。path が None なら既定値だけで構成する。"""
    data: dict = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{path} を読み込めません: {e}") from e

    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"不明なセクションがあります: {', '.join(sorted(unknown))}")

    sections = {}
    for name, cls in _SECTIONS.items():
        raw = data.get(name, {})
        try:
            sections[name] = cls(**raw)
        except TypeError as e:
            raise ConfigError(f"[{name}] の項目が不正です: {e}") from e
    config = Config(**sections)

    if config.access.enabled and not (config.access.team_domain and config.access.aud):
        raise ConfigError("[access] enabled = true の場合は team_domain と aud が必要です")
    if "{q}" not in config.search.url:
        raise ConfigError("[search] url には {q} を含めてください")
    if config.image.default_quality not in ("low", "mid", "high", "orig"):
        raise ConfigError("[image] default_quality は low / mid / high / orig のいずれかにしてください")
    return config
