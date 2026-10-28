from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from rss2discord.discord.client import SleepCallback
from rss2discord.models import EntryId
from rss2discord.providers.pazar3.http import Pazar3ScanBudget, fetch_pazar3_page
from rss2discord.providers.pazar3.models import Pazar3Listing, Pazar3Page
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.pazar3.parser import parse_pazar3_page
from rss2discord.providers.pazar3.scope import (
    MAX_PAZAR3_CATALOG_PAGES,
    PAZAR3_LABEL,
    Pazar3SearchScope,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError

MAX_PAZAR3_CATALOG_LISTINGS: Final = 500
PAZAR3_RESULTS_PER_PAGE: Final = 50
type Pazar3CatalogClock = Callable[[], datetime]


class Pazar3CatalogClient:
    def __init__(
        self,
        pacer: Pazar3RequestPacer,
        sleep: SleepCallback,
        *,
        clock: Pazar3CatalogClock | None = None,
    ) -> None:
        self._pacer = pacer
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Pazar3Listing, ...]:
        scope = Pazar3SearchScope.from_url(url)
        return retry_policy.execute(
            lambda: self._scan_catalog(scope, is_shutdown_requested),
        )

    def _scan_catalog(
        self,
        scope: Pazar3SearchScope,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Pazar3Listing, ...]:
        budget = Pazar3ScanBudget.for_catalog()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise FeedFetchError(PAZAR3_LABEL, "InvalidClock")
        seen_ids: set[EntryId] = set()
        listings: dict[EntryId, Pazar3Listing] = {}
        expected_result_count: int | None = None
        organic_row_count = 0
        for page_number in range(1, MAX_PAZAR3_CATALOG_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            request = scope.catalog_page_request(page_number)
            page = parse_pazar3_page(
                fetch_pazar3_page(
                    request,
                    budget,
                    self._pacer,
                    self._sleep,
                    is_shutdown_requested,
                ),
                request,
                now,
            )
            if expected_result_count is None:
                expected_result_count = page.result_count
                if expected_result_count > MAX_PAZAR3_CATALOG_LISTINGS:
                    raise FeedFetchError(PAZAR3_LABEL, "ProductLimitExceeded")
            elif page.result_count != expected_result_count:
                raise FeedFetchError(
                    PAZAR3_LABEL,
                    "ChangedResultCount",
                    retryable=True,
                )
            _validate_catalog_page(page, page_number, seen_ids)
            _merge_listings(page.listings, listings)
            seen_ids.update(page.organic_ids)
            organic_row_count += page.organic_row_count
            if page.terminal:
                _require_complete_catalog(
                    organic_row_count,
                    len(seen_ids),
                    expected_result_count,
                    is_shutdown_requested,
                )
                return tuple(listings.values())
        raise FeedFetchError(PAZAR3_LABEL, "PageLimitExceeded")


def _validate_catalog_page(
    page: Pazar3Page,
    page_number: int,
    seen_ids: set[EntryId],
) -> None:
    if page_number > 1 and page.organic_ids & seen_ids:
        raise FeedFetchError(PAZAR3_LABEL, "PaginationCycle", retryable=True)
    if not page.terminal and page.organic_row_count != PAZAR3_RESULTS_PER_PAGE:
        raise FeedFetchError(PAZAR3_LABEL, "IncompletePage", retryable=True)
    if not page.organic_ids and not page.terminal:
        raise FeedFetchError(PAZAR3_LABEL, "EmptyNonTerminalPage", retryable=True)


def _merge_listings(
    incoming: tuple[Pazar3Listing, ...],
    listings: dict[EntryId, Pazar3Listing],
) -> None:
    for listing in incoming:
        existing = listings.get(listing.entry_id)
        if existing is not None and existing != listing:
            raise FeedFetchError(PAZAR3_LABEL, "ConflictingProductId")
        listings.setdefault(listing.entry_id, listing)


def _require_complete_catalog(
    row_count: int,
    unique_count: int,
    expected_count: int,
    is_shutdown_requested: Callable[[], bool],
) -> None:
    if row_count != expected_count or unique_count != expected_count:
        raise FeedFetchError(PAZAR3_LABEL, "IncompleteCatalog", retryable=True)
    if is_shutdown_requested():
        raise FeedFetchInterruptedError
