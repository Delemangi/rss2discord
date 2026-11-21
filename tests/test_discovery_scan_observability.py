"""Discovery-scan summary evidence."""

import logging
from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig
from rss2discord.delivery_store import DeliveryStore
from tests.app_helpers import (
    FakeSender,
    FakeStrategy,
    make_app,
    make_entry,
    make_feed,
)

APP_LOGGER = "rss2discord.app"


def test_discovery_log_counts_new_and_already_delivered_entries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given one already-delivered entry and one new entry
    feed = make_feed("observability")
    strategy = FakeStrategy([make_entry("already-delivered"), make_entry("new")])
    sender = FakeSender([True])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.mark_delivered(feed.id, "already-delivered")
        app = make_app(store, sender, strategy, (feed,))

        # When
        with caplog.at_level(logging.INFO, logger=APP_LOGGER):
            app.process_feed(feed)

        # Then
        assert "Discovery scan feed=observability strategy=rss entries=2" in (
            caplog.text
        )
        assert "unhandled=1" in caplog.text
        assert "pending=1" in caplog.text
        assert "delivered=1" in caplog.text
        assert "failed=0" in caplog.text
        assert "duplicates=1" in caplog.text
        assert "seeded=false" in caplog.text
        assert "digest=false" in caplog.text
        assert "state=healthy" in caplog.text


def test_discovery_log_marks_a_seeded_baseline(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a strategy that seeds its current entries on the first fetch
    feed = make_feed("seeded")
    strategy = FakeStrategy([make_entry("one"), make_entry("two")])
    strategy.seed_existing_on_first_fetch = True
    sender = FakeSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))

        # When
        with caplog.at_level(logging.INFO, logger=APP_LOGGER):
            app.process_feed(feed)

        # Then
        assert "entries=2" in caplog.text
        assert "pending=0" in caplog.text
        assert "delivered=0" in caplog.text
        assert "seeded=true" in caplog.text
        assert sender.messages == []


def test_discovery_log_counts_too_old_entries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a feed configured to skip entries older than one day
    feed = make_feed("old")
    strategy = FakeStrategy([make_entry("old-entry")])
    sender = FakeSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(
            config=AppConfig(
                delay_between_posts=0,
                max_post_age_days=1,
                feeds=(feed,),
            ),
            store=store,
            sender=sender,
        )
        app._strategies["rss"] = strategy

        # When
        with caplog.at_level(logging.INFO, logger=APP_LOGGER):
            app.process_feed(feed)

        # Then
        assert "entries=1" in caplog.text
        assert "pending=0" in caplog.text
        assert "too_old=1" in caplog.text
        assert "delivered=0" in caplog.text
