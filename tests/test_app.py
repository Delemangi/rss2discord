from pathlib import Path
from typing import Any

import pytest

from app import RSSToDiscord
from configuration import AppConfig, FeedConfig
from delivery_store import DeliveryStore
from tests.app_helpers import (
    FakeEntry,
    FakeSender,
    FakeStrategy,
    make_app,
    make_entry,
    make_feed,
)


class FakeStrategyWithTitle(FakeStrategy):
    def __init__(self, entries: list[FakeEntry], fetched_title: str) -> None:
        super().__init__(entries)
        self._fetched_title = fetched_title

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        return list(self.entries), self._fetched_title


def test_run_waits_between_feeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feeds = (make_feed("first"), make_feed("second"))
    config = AppConfig(
        refresh_interval=60,
        delay_between_feeds=61,
        delay_between_posts=0,
        max_post_age_days=0,
        feeds=feeds,
    )
    sender = FakeSender([])
    strategy = FakeStrategy([])
    sleep_calls: list[float] = []

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config=config, store=store, sender=sender)
        app._strategies["rss"] = strategy

        def record_sleep(seconds: float) -> bool:
            sleep_calls.append(seconds)
            if seconds == config.refresh_interval:
                app.request_shutdown()
                return False
            return True

        monkeypatch.setattr(app, "_interruptible_sleep", record_sleep)

        # When
        app.run()

    # Then
    assert sleep_calls == [61, 60]


def test_same_url_delivers_independently_for_each_feed_id(tmp_path: Path) -> None:
    # Given
    primary = make_feed("primary")
    secondary = make_feed("secondary")
    sender = FakeSender([True, True])
    strategy = FakeStrategy([make_entry("entry-1")])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (primary, secondary))
        app.process_feed(primary)
        app.process_feed(secondary)

        # Then
        assert store.has_delivered("primary", "entry-1")
        assert store.has_delivered("secondary", "entry-1")
    assert [message.feed.id for message in sender.messages] == [
        "primary",
        "secondary",
    ]


def test_failed_delivery_is_retried_and_only_success_is_recorded(
    tmp_path: Path,
) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([False, True])
    strategy = FakeStrategy([make_entry("entry-1")])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        app.process_feed(feed)
        first_attempt_delivered = store.has_delivered("news", "entry-1")
        app.process_feed(feed)

        # Then
        assert not first_attempt_delivered
        assert store.has_delivered("news", "entry-1")
    assert len(sender.messages) == 2


def test_duplicate_entries_in_one_fetch_are_sent_once(tmp_path: Path) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([True])
    strategy = FakeStrategy([make_entry("entry-1"), make_entry("entry-1")])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        app.process_feed(feed)

    # Then
    assert len(sender.messages) == 1


def test_success_is_committed_before_later_delivery_crashes(tmp_path: Path) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([True, RuntimeError("transport crashed")])
    strategy = FakeStrategy([make_entry("entry-1"), make_entry("entry-2")])
    database_path = tmp_path / "state.db"

    # When
    with DeliveryStore(database_path) as store:
        app = make_app(store, sender, strategy, (feed,))
        with pytest.raises(RuntimeError, match="transport crashed"):
            app.process_feed(feed)

    # Then
    with DeliveryStore(database_path) as reopened_store:
        assert reopened_store.has_delivered("news", "entry-1")
        assert not reopened_store.has_delivered("news", "entry-2")


def test_configured_feed_name_wins_over_fetched_source_title(tmp_path: Path) -> None:
    # Given
    feed = FeedConfig(
        id="news",
        name="Configured Name",
        url="https://example.test/feed.xml",
        webhook="https://discord.test/news",
    )
    sender = FakeSender([True])
    strategy = FakeStrategyWithTitle(
        [make_entry("entry-1")],
        fetched_title="Fetched Title",
    )

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        app.process_feed(feed)

    # Then
    assert len(sender.messages) == 1
    assert sender.messages[0].source_title == "Configured Name"
