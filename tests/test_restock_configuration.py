"""Feed configuration for restock alerts."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.configuration import (
    RESTOCK_STRATEGIES,
    FeedConfig,
    load_config,
)


def make_feed(strategy: str = "anhoch", **overrides: object) -> FeedConfig:
    values: dict[str, object] = {
        "id": "feed",
        "url": "https://shop.example.test/feed",
        "webhook": "https://discord.example.test/hook",
        "strategy": strategy,
    }
    values.update(overrides)
    return FeedConfig.model_validate(values)


def test_restock_alerts_defaults_to_false() -> None:
    assert make_feed().restock_alerts is False


def test_restock_strategies_are_the_stock_aware_providers() -> None:
    assert {"ananas", "anhoch", "cccenter"} == RESTOCK_STRATEGIES


@pytest.mark.parametrize("strategy", sorted(RESTOCK_STRATEGIES))
def test_restock_alerts_is_accepted_for_stock_aware_strategies(strategy: str) -> None:
    assert make_feed(strategy, restock_alerts=True).restock_alerts is True


@pytest.mark.parametrize("strategy", ["rss", "neptun", "ddstore"])
def test_restock_alerts_is_rejected_without_a_stock_aware_strategy(
    strategy: str,
) -> None:
    with pytest.raises(ValidationError, match="stock-aware price strategy"):
        make_feed(strategy, restock_alerts=True)


def test_restock_alerts_loads_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "feeds:\n"
        "  - id: restock\n"
        "    url: https://shop.example.test/restock\n"
        "    webhook: https://discord.example.test/hook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: 3600\n"
        "    restock_alerts: true\n"
        "  - id: plain\n"
        "    url: https://shop.example.test/plain\n"
        "    webhook: https://discord.example.test/hook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: 3600\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [feed.restock_alerts for feed in config.feeds] == [True, False]
