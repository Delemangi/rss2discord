from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.transports.ddstore import DDStoreStrategy
from tests.app_helpers import FakeSender
from tests.test_ddstore_price_monitor import make_product


def test_ddstore_rejects_delivery_history_growth_at_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = FeedConfig(
        id="ddstore-products",
        url="https://ddstore.mk/",
        webhook="https://discord.test/ddstore",
        strategy="ddstore",
    )
    product = make_product("new")
    monkeypatch.setattr(
        DDStoreStrategy,
        "fetch_entries",
        lambda strategy, url: ([product], "DDStore"),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        store.seed_feed(feed.id, (str(index) for index in range(50_000)))
        sender = FakeSender([True])
        app = RSSToDiscord(
            AppConfig(max_post_age_days=0, feeds=(feed,)),
            store,
            sender,
        )

        # When / Then
        with pytest.raises(FeedFetchError) as error_info:
            app.process_feed(feed)
        assert error_info.value.strategy == "Feed"
        assert error_info.value.cause_type == "DeliveryHistoryLimitExceeded"
        assert sender.messages == []
        assert not store.has_delivered(feed.id, "new")


def test_ddstore_accepts_delivery_history_growth_to_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = FeedConfig(
        id="ddstore-products",
        url="https://ddstore.mk/",
        webhook="https://discord.test/ddstore",
        strategy="ddstore",
    )
    product = make_product("new")
    monkeypatch.setattr(
        DDStoreStrategy,
        "fetch_entries",
        lambda strategy, url: ([product], "DDStore"),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        store.seed_feed(feed.id, (str(index) for index in range(49_999)))
        sender = FakeSender([True])
        app = RSSToDiscord(
            AppConfig(max_post_age_days=0, feeds=(feed,)),
            store,
            sender,
        )

        # When
        app.process_feed(feed)

        # Then
        assert len(sender.messages) == 1
        assert store.has_delivered(feed.id, "new")


def test_ddstore_rejects_oversized_legacy_delivery_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = FeedConfig(
        id="ddstore-products",
        url="https://ddstore.mk/",
        webhook="https://discord.test/ddstore",
        strategy="ddstore",
    )
    product = make_product("0")
    monkeypatch.setattr(
        DDStoreStrategy,
        "fetch_entries",
        lambda strategy, url: ([product], "DDStore"),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        store.seed_feed(feed.id, (str(index) for index in range(50_001)))
        sender = FakeSender([])
        app = RSSToDiscord(
            AppConfig(max_post_age_days=0, feeds=(feed,)),
            store,
            sender,
        )

        # When / Then
        with pytest.raises(FeedFetchError) as error_info:
            app.process_feed(feed)
        assert error_info.value.cause_type == "DeliveryHistoryLimitExceeded"
        assert sender.messages == []
