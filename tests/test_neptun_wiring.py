from pathlib import Path

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import source_label
from rss2discord.transports import NeptunStrategy
from tests.app_helpers import FakeSender


def make_feed(*, interval: float | None = None) -> FeedConfig:
    return FeedConfig(
        id="neptun-computers",
        url="https://www.neptun.mk/KOMPJUTERI.nspx",
        webhook="https://discord.test/webhooks/id/token",
        strategy="neptun",
        price_check_interval=interval,
    )


def test_neptun_configuration_accepts_optional_positive_price_interval() -> None:
    assert make_feed().price_check_interval is None
    assert make_feed(interval=3600).price_check_interval == 3600


def test_app_registers_neptun_strategy(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(), store, FakeSender([]))

    assert isinstance(app._strategies["neptun"], NeptunStrategy)


def test_source_label_returns_neptun() -> None:
    assert source_label(make_feed()) == "Neptun"
