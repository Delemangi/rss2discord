from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import source_label
from rss2discord.transports import Gjirafa50Strategy
from tests.app_helpers import FakeSender


def make_feed(*, interval: float | None = None) -> FeedConfig:
    return FeedConfig(
        id="gjirafa50-products",
        url="https://gjirafa50.mk/",
        webhook="https://discord.test/webhooks/id/token",
        strategy="gjirafa50",
        price_check_interval=interval,
    )


def test_app_registers_gjirafa50_strategy(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(), store, FakeSender([]))

    assert isinstance(app._strategies["gjirafa50"], Gjirafa50Strategy)


def test_source_label_returns_gjirafa50() -> None:
    assert source_label(make_feed()) == "Gjirafa50"


def test_configuration_rejects_gjirafa50_price_interval_below_six_hours() -> None:
    with pytest.raises(ValidationError, match="21600"):
        make_feed(interval=3600)
