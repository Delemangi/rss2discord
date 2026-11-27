"""Discovery entry tracker: dedupe, watermark filtering, and touch state."""

from dataclasses import dataclass
from typing import Any

from rss2discord.discovery_entries import DiscoveryEntryTracker
from rss2discord.models import EntryId
from rss2discord.transports.base import ScraperStrategy


@dataclass
class Counters:
    missing_id: int = 0
    duplicates: int = 0
    watermark_skipped: int = 0
    unhandled: int = 0


class StubStrategy(ScraperStrategy):
    def __init__(self, entry_id: str | None) -> None:
        self.entry_id = entry_id

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        return [], "Stub"

    def get_entry_id(self, entry: Any) -> EntryId | None:  # noqa: ANN401
        if self.entry_id is None:
            return None
        return EntryId(self.entry_id)

    def get_entry_data(self, entry: Any) -> Any:  # noqa: ANN401
        raise AssertionError("not used")


def test_tracker_skips_delivered_and_records_touch() -> None:
    counters = Counters()
    tracker = DiscoveryEntryTracker(
        feed_id="feed",
        delivered={"10"},
    )

    assert tracker.unhandled_entry_id(StubStrategy("10"), object(), counters) is None
    assert counters.duplicates == 1
    assert tracker.delivered_seen == {"10"}


def test_tracker_skips_seen_duplicate_without_touching() -> None:
    counters = Counters()
    tracker = DiscoveryEntryTracker(feed_id="feed", delivered=set())
    strategy = StubStrategy("20")

    assert tracker.unhandled_entry_id(strategy, object(), counters) == "20"
    assert tracker.unhandled_entry_id(strategy, object(), counters) is None
    assert counters.unhandled == 1
    assert counters.duplicates == 1
    assert tracker.delivered_seen == set()


def test_tracker_skips_entry_below_watermark() -> None:
    counters = Counters()
    tracker = DiscoveryEntryTracker(
        feed_id="feed",
        delivered=set(),
        watermark=30,
        uses_monotonic_entry_ids=True,
    )

    assert tracker.unhandled_entry_id(StubStrategy("30"), object(), counters) is None
    assert counters.watermark_skipped == 1
    assert tracker.delivered_seen == set()


def test_tracker_accepts_entry_above_watermark() -> None:
    counters = Counters()
    tracker = DiscoveryEntryTracker(
        feed_id="feed",
        delivered=set(),
        watermark=30,
        uses_monotonic_entry_ids=True,
    )

    assert tracker.unhandled_entry_id(StubStrategy("31"), object(), counters) == "31"
    assert counters.unhandled == 1
    assert counters.watermark_skipped == 0


def test_tracker_counts_missing_id() -> None:
    counters = Counters()
    tracker = DiscoveryEntryTracker(feed_id="feed", delivered=set())

    assert tracker.unhandled_entry_id(StubStrategy(None), object(), counters) is None
    assert counters.missing_id == 1
    assert tracker.delivered_seen == set()
