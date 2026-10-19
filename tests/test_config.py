from pathlib import Path

import pytest

from liteproxy.config import Config, ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_example_matches_defaults():
    # 設定例とコード上の既定値がずれないようにする
    assert load_config(ROOT / "config.example.toml") == Config()


def test_unknown_key_is_rejected(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[browser]\nno_such_key = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_access_requires_team_and_aud(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[access]\nenabled = true\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "body",
    [
        "keep_minutes = 0",
        "max_results = 0",
        "reuse_minutes = -1",
        "reuse_minutes = 40\nkeep_minutes = 30",
    ],
)
def test_invalid_cache_settings_are_rejected(tmp_path, body):
    path = tmp_path / "c.toml"
    path.write_text(f"[cache]\n{body}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
