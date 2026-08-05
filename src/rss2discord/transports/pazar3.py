import time
from collections.abc import Callable
from datetime import UTC, datetime

from rss2discord.discord.client import SleepCallback
from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import FeedFetchError, ScraperStrategy
from rss2discord.transports.pazar3_http import (
    Pazar3ScanBudget,
    fetch_pazar3_page,
)
from rss2discord.transports.pazar3_models import Pazar3Listing, Pazar3Page
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer
from rss2discord.transports.pazar3_parser import parse_pazar3_page
from rss2discord.transports.pazar3_scope import PAZAR3_LABEL, Pazar3SearchScope

type Pazar3Clock = Callable[[], datetime]


def _blocking_sleep(seconds: float) -> bool:
    time.sleep(seconds)
    return True


class Pazar3Strategy(ScraperStrategy):
    seed_existing_on_first_fetch = True
    require_entries_for_initialization = False
    max_new_entries_per_fetch = None
    max_delivery_history = None

    def __init__(
        self,
        clock: Pazar3Clock | None = None,
        pacer: Pazar3RequestPacer | None = None,
        sleep: SleepCallback = _blocking_sleep,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pacer = pacer or Pazar3RequestPacer(time.monotonic)
        self._sleep = sleep
        self._is_shutdown_requested = is_shutdown_requested
        self._observed_organic_ids: frozenset[EntryId] = frozenset()

    def fetch_entries(self, url: str) -> tuple[list[Pazar3Listing], str]:
        scope = Pazar3SearchScope.from_url(url)
        budget = Pazar3ScanBudget.for_attempt()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise FeedFetchError(PAZAR3_LABEL, "InvalidClock")
        seen_ids: set[EntryId] = set()
        retained: dict[EntryId, tuple[Pazar3Listing, int]] = {}
        scan_position = 0
        for page_number in range(1, 4):
            if self._is_shutdown_requested():
                raise FeedFetchInterruptedError
            request = scope.page_request(page_number)
            page = parse_pazar3_page(
                fetch_pazar3_page(
                    request,
                    budget,
                    self._pacer,
                    self._sleep,
                    self._is_shutdown_requested,
                ),
                request,
                now,
            )
            if page_number > 1 and page.organic_ids and page.organic_ids <= seen_ids:
                raise FeedFetchError(PAZAR3_LABEL, "PaginationCycle")
            if page_number < 3 and not page.organic_ids and not page.terminal:
                raise FeedFetchError(PAZAR3_LABEL, "EmptyNonTerminalPage")
            for listing in page.listings:
                retained.setdefault(listing.entry_id, (listing, scan_position))
                scan_position += 1
            seen_ids.update(page.organic_ids)
            if page.terminal:
                break
        if self._is_shutdown_requested():
            raise FeedFetchInterruptedError
        entries = [
            listing
            for listing, _position in sorted(
                retained.values(),
                key=lambda retained_listing: (
                    retained_listing[0].activity_at,
                    retained_listing[1],
                ),
            )
        ]
        self._observed_organic_ids = frozenset(seen_ids)
        return entries, PAZAR3_LABEL

    def get_initialization_entry_ids(
        self,
        entries: list[Pazar3Listing],
    ) -> set[EntryId]:
        return super().get_initialization_entry_ids(entries) | set(
            self._observed_organic_ids,
        )

    def get_entry_id(self, entry: Pazar3Listing) -> EntryId:
        return entry.entry_id

    def get_entry_data(self, entry: Pazar3Listing) -> EntryData:
        metrics = tuple(
            metric
            for metric in (
                SourceMetric("Price", entry.price) if entry.price else None,
                SourceMetric("Location", entry.location) if entry.location else None,
            )
            if metric is not None
        )
        return EntryData(
            title=entry.title,
            link=entry.url,
            description="",
            author="",
            timestamp=entry.activity_at.isoformat(),
            image_url=entry.image_url,
            categories=(entry.category,) if entry.category else (),
            source_metrics=metrics,
        )


__all__ = (
    "PAZAR3_LABEL",
    "Pazar3Listing",
    "Pazar3Page",
    "Pazar3Strategy",
)
