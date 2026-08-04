from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from rss2discord.models import EntryId
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.reklama5_http import (
    Reklama5ScanBudget,
    fetch_reklama5_page,
)
from rss2discord.transports.reklama5_parser import (
    Reklama5Listing,
    parse_reklama5_page,
)
from rss2discord.transports.reklama5_scope import (
    MAX_REKLAMA5_CATALOG_PAGES,
    REKLAMA5_LABEL,
    Reklama5SearchScope,
)

MAX_REKLAMA5_CATALOG_LISTINGS: Final = 10_000
type Reklama5CatalogClock = Callable[[], datetime]


class Reklama5CatalogClient:
    def __init__(self, clock: Reklama5CatalogClock | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Reklama5Listing, ...]:
        scope = Reklama5SearchScope.from_url(url)
        return retry_policy.execute(
            lambda: self._scan_catalog(scope, is_shutdown_requested),
        )

    def _scan_catalog(
        self,
        scope: Reklama5SearchScope,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Reklama5Listing, ...]:
        budget = Reklama5ScanBudget.for_catalog()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidClock")
        seen_ids: set[EntryId] = set()
        listings: dict[EntryId, Reklama5Listing] = {}
        expected_result_count: int | None = None
        organic_row_count = 0
        for page_number in range(1, MAX_REKLAMA5_CATALOG_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            request = scope.catalog_page_request(page_number)
            page = parse_reklama5_page(
                fetch_reklama5_page(request, budget),
                request,
                now,
            )
            if expected_result_count is None:
                expected_result_count = page.result_count
            elif page.result_count != expected_result_count:
                raise FeedFetchError(
                    REKLAMA5_LABEL,
                    "ChangedResultCount",
                    retryable=True,
                )
            if page_number > 1 and page.organic_ids and page.organic_ids <= seen_ids:
                raise FeedFetchError(
                    REKLAMA5_LABEL,
                    "PaginationCycle",
                    retryable=True,
                )
            if not page.organic_ids and not page.terminal:
                raise FeedFetchError(
                    REKLAMA5_LABEL,
                    "EmptyNonTerminalPage",
                    retryable=True,
                )
            if len(seen_ids | page.organic_ids) > MAX_REKLAMA5_CATALOG_LISTINGS:
                raise FeedFetchError(REKLAMA5_LABEL, "ProductLimitExceeded")
            for listing in page.listings:
                listings.setdefault(listing.entry_id, listing)
            seen_ids.update(page.organic_ids)
            organic_row_count += page.organic_row_count
            if page.terminal:
                if organic_row_count != expected_result_count:
                    raise FeedFetchError(
                        REKLAMA5_LABEL,
                        "IncompleteCatalog",
                        retryable=True,
                    )
                if is_shutdown_requested():
                    raise FeedFetchInterruptedError
                return tuple(listings.values())
        raise FeedFetchError(REKLAMA5_LABEL, "PageLimitExceeded")
