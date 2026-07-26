from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.configuration import load_config


def write_config(path: Path, feed: str) -> None:
    path.write_text(
        "refresh_interval: 60\n"
        "delay_between_posts: 0\n"
        "max_post_age_days: 7\n"
        f"feeds:\n{feed}",
    )


def test_load_config_parses_neksio_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: neksio-products\n"
        "    url: https://g.store.neksio.mk/\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: neksio\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].strategy == "neksio"


def test_load_config_rejects_price_check_interval_for_neksio_feed(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: neksio-products\n"
        "    url: https://g.store.neksio.mk/\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: neksio\n"
        "    price_check_interval: 3600\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)
