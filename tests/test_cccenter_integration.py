from collections.abc import Callable
from pathlib import Path

import pytest

from rss2discord.configuration import load_config
from rss2discord.providers.cccenter.strategy import CCCenterStrategy


def test_config_example_documents_cccenter() -> None:
    config = load_config(
        Path(__file__).parent.parent / "config" / "config.example.yaml",
    )
    feed = next(feed for feed in config.feeds if feed.id == "cccenter-products")
    assert feed.strategy == "cccenter"
    assert feed.price_check_interval == 3600


def test_cccenter_strategy_passes_app_shutdown_callback_to_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def shutdown() -> bool:
        return True

    strategy = CCCenterStrategy(shutdown)
    observed: list[Callable[[], bool]] = []

    def fetch(
        url: str,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[object, ...]:
        del url
        observed.append(is_shutdown_requested)
        return ()

    monkeypatch.setattr(strategy._client, "fetch_latest_products", fetch)
    strategy.fetch_entries("https://cccenter.mk/shop/?orderby=date")

    assert observed == [shutdown]
