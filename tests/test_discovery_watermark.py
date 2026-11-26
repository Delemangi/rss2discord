"""Monotonic discovery watermarks for update-ordered provider listings."""

import logging
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from tests.app_helpers import (
    FakeSender,
    FakeStrategy,
    make_app,
    make_entry,
    make_feed,
)

APP_LOGGER = "rss2discord.app"


def _monotonic_strategy(entries: list) -> FakeStrategy:
    strategy = FakeStrategy(entries)
    strategy.uses_monotonic_entry_ids = True
    return strategy


def test_existing_feed_initializes_watermark_without_replaying_entries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given an initialized feed with no watermark yet
    feed = make_feed("anhoch")
    strategy = _monotonic_strategy(
        [make_entry("10"), make_entry("20"), make_entry("30")],
    )
    sender = FakeSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.seed_feed(feed.id, ())
        app = make_app(store, sender, strategy, (feed,))

        # When
        with caplog.at_level(logging.INFO, logger=APP_LOGGER):
            app.process_feed(feed)

        # Then the current update-ordered window is baselined silently
        assert store.load_discovery_watermark(feed.id) == 30
        assert sender.messages == []
        assert "watermark_skipped=3" in caplog.text


def test_watermark_delivers_only_newer_entry_ids(tmp_path: Path) -> None:
    # Given a feed whose known high-water mark is 20
    feed = make_feed("anhoch")
    strategy = _monotonic_strategy(
        [make_entry("10"), make_entry("20"), make_entry("30")],
    )
    sender = FakeSender([True])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.store_discovery_watermark(feed.id, 20)
        app = make_app(store, sender, strategy, (feed,))

        # When
        app.process_feed(feed)

        # Then only the genuinely newer ID is delivered
        assert [message.entry.title for message in sender.messages] == ["30"]
        assert store.load_discovery_watermark(feed.id) == 30


def test_failed_delivery_does_not_advance_watermark(tmp_path: Path) -> None:
    # Given a new entry whose first delivery fails
    feed = make_feed("anhoch")
    strategy = _monotonic_strategy([make_entry("10"), make_entry("20")])
    failed_sender = FakeSender([False])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.store_discovery_watermark(feed.id, 10)
        app = make_app(store, failed_sender, strategy, (feed,))

        # When
        app.process_feed(feed)

        # Then the entry remains retryable
        assert store.load_discovery_watermark(feed.id) == 10
        assert not store.has_delivered(feed.id, "20")

        # When the next scan succeeds
        retry_sender = FakeSender([True])
        retry_app = make_app(store, retry_sender, strategy, (feed,))
        retry_app.process_feed(feed)

        # Then it is delivered and the watermark advances
        assert store.load_discovery_watermark(feed.id) == 20
        assert store.has_delivered(feed.id, "20")


def test_first_fetch_seeds_and_records_watermark(tmp_path: Path) -> None:
    # Given a fresh feed that seeds its current entries
    feed = make_feed("anhoch", seed_existing_on_first_fetch=True)
    strategy = _monotonic_strategy([make_entry("10"), make_entry("20")])
    strategy.seed_existing_on_first_fetch = True
    sender = FakeSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))

        # When
        app.process_feed(feed)

        # Then the baseline is silent and the watermark starts at its maximum
        assert sender.messages == []
        assert store.has_delivered(feed.id, "10")
        assert store.load_discovery_watermark(feed.id) == 20


def test_non_monotonic_strategy_ignores_persisted_watermark(tmp_path: Path) -> None:
    # Given a watermark that must not affect ordinary strategies
    feed = make_feed("rss")
    strategy = FakeStrategy([make_entry("10"), make_entry("20")])
    sender = FakeSender([True, True])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.store_discovery_watermark(feed.id, 20)
        app = make_app(store, sender, strategy, (feed,))

        # When
        app.process_feed(feed)

        # Then both entries are delivered normally
        assert [message.entry.title for message in sender.messages] == ["10", "20"]


def test_discovery_watermark_never_lowers(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.store_discovery_watermark("feed", 10)
        store.store_discovery_watermark("feed", 5)

        assert store.load_discovery_watermark("feed") == 10

        store.store_discovery_watermark("feed", 20)
        assert store.load_discovery_watermark("feed") == 20
