from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.transports.base import FeedFetchError, ScraperStrategy
from rss2discord.transports.reklama5_http import (
    Reklama5ScanBudget,
    Reklama5SearchScope,
    fetch_reklama5_page,
)
from rss2discord.transports.reklama5_parser import (
    Reklama5Listing,
    parse_reklama5_page,
)
from rss2discord.transports.reklama5_scope import REKLAMA5_LABEL

type Reklama5Clock = Callable[[], datetime]


class Reklama5Strategy(ScraperStrategy):
    seed_existing_on_first_fetch = True
    require_entries_for_initialization = False
    max_new_entries_per_fetch = None
    max_delivery_history = None

    def __init__(self, clock: Reklama5Clock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._observed_organic_ids: frozenset[EntryId] = frozenset()

    def fetch_entries(self, url: str) -> tuple[list[Reklama5Listing], str]:
        scope = Reklama5SearchScope.from_url(url)
        budget = Reklama5ScanBudget.for_attempt()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidClock")

        seen_ids: set[EntryId] = set()
        retained: dict[EntryId, tuple[Reklama5Listing, int]] = {}
        scan_position = 0
        for page_number in range(1, 4):
            request = scope.page_request(page_number)
            page = parse_reklama5_page(
                fetch_reklama5_page(request, budget),
                request,
                now,
            )
            if (
                page_number > 1
                and page.organic_ids
                and page.organic_ids <= seen_ids
            ):
                raise FeedFetchError(REKLAMA5_LABEL, "PaginationCycle")
            if page_number < 3 and not page.organic_ids and not page.terminal:
                raise FeedFetchError(REKLAMA5_LABEL, "EmptyNonTerminalPage")

            for listing in page.listings:
                retained.setdefault(
                    listing.entry_id,
                    (listing, scan_position),
                )
                scan_position += 1
            seen_ids.update(page.organic_ids)
            if page.terminal:
                break

        entries = [
            listing
            for listing, _position in sorted(
                retained.values(),
                key=lambda retained_listing: (
                    retained_listing[0].activity_at,
                    -retained_listing[1],
                ),
            )
        ]
        self._observed_organic_ids = frozenset(seen_ids)
        return entries, REKLAMA5_LABEL

    def get_initialization_entry_ids(self, entries: list[Any]) -> set[EntryId]:
        return super().get_initialization_entry_ids(entries) | set(
            self._observed_organic_ids,
        )

    def get_entry_id(self, entry: Reklama5Listing) -> EntryId:
        return entry.entry_id

    def get_entry_data(self, entry: Reklama5Listing) -> EntryData:
        metrics: list[SourceMetric] = []
        if entry.price:
            metrics.append(SourceMetric(label="Price", value=entry.price))
        if entry.location:
            metrics.append(SourceMetric(label="Location", value=entry.location))
        return EntryData(
            title=entry.title,
            link=entry.url,
            description=entry.summary,
            author="",
            timestamp=entry.activity_at.isoformat(),
            image_url=entry.image_url,
            categories=(entry.category,) if entry.category else (),
            source_metrics=tuple(metrics),
        )
