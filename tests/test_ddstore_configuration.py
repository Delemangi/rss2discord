from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.configuration import load_config
from tests.configuration_helpers import write_config


def test_load_config_parses_ddstore_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """  - id: ddstore-new-products
    url: https://ddstore.mk/
    webhook: https://discord.test/webhook
    strategy: ddstore
""",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].strategy == "ddstore"
    assert config.feeds[0].price_check_interval is None


def test_load_config_parses_positive_ddstore_price_check_interval(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """  - id: ddstore
    url: https://ddstore.mk/
    webhook: https://discord.test/webhook
    strategy: ddstore
    price_check_interval: 3600
""",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval == 3600


def test_load_config_rejects_price_check_interval_for_rss_with_ddstore_in_error(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        """  - id: unsupported
    url: https://example.test/feed.xml
    webhook: https://discord.test/webhook
    strategy: rss
    price_check_interval: 3600
""",
    )

    # When / Then
    with pytest.raises(
        ValidationError,
        match=(
            "price_check_interval requires the anhoch, ddstore, neksio, neptun, "
            "reklama5, or setec strategy"
        ),
    ):
        _ = load_config(config_path)


def test_checked_in_config_example_enables_hourly_ddstore_price_monitoring() -> None:
    # Given
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    # When
    config = load_config(example_path)
    ddstore_feed = next(
        feed for feed in config.feeds if feed.id == "ddstore-new-products"
    )

    # Then
    assert ddstore_feed.strategy == "ddstore"
    assert ddstore_feed.price_check_interval == 3600
