"""Generic freshness health evidence for discovery feeds."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryData, EntryId
from tests.app_helpers import (
    FakeEntry,
    FakeSender,
    FakeStrategy,
    make_app,
    make_entry,
    make_feed,
)


def test_old_newest_entry_marks_the_feed_stale(tmp_path: Path) -> None:
    # Given a feed whose newest source entry is older than its threshold
    feed = make_feed("stale", stale_after_days=1)
    sender = FakeSender([True])
    strategy = FakeStrategy([make_entry("old")])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))

        # When
        app.process_feed(feed)

        # Then
        health = store.list_health(feed.id)[0]
        assert (health.state, health.cause) == ("stale", "NoRecentEntries")
        assert store.has_delivered(feed.id, "old")


def test_recent_newest_entry_stays_healthy(tmp_path: Path) -> None:
    # Given a feed whose newest source entry is current
    feed = make_feed("fresh", stale_after_days=1)
    sender = FakeSender([True])
    entry = FakeEntry(
        id=EntryId("fresh"),
        data=EntryData(
            title="fresh",
            link="https://example.test/fresh",
            description="",
            author="",
            timestamp=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        ),
    )
    strategy = FakeStrategy([entry])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))

        # When
        app.process_feed(feed)

        # Then
        health = store.list_health(feed.id)[0]
        assert health.state == "healthy"
