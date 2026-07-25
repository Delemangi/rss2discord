"""Immutable bounds for one Anhoch catalog scan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from rss2discord.transports.anhoch_models import AnhochProductPage
from rss2discord.transports.base import FeedFetchError

ANHOCH_LABEL: Final = "Anhoch"


@dataclass(frozen=True, slots=True)
class FetchedCatalogPage:
    """One validated page and every response byte consumed to reach it."""

    page: AnhochProductPage
    response_bytes: int


@dataclass(frozen=True, slots=True)
class CatalogScanTotals:
    """Immutable accumulated product and response-byte counts."""

    product_count: int
    response_bytes: int


@dataclass(frozen=True, slots=True)
class CatalogScanBounds:
    """Validate catalog page metadata before its products are retained."""

    per_page: int
    max_pages: int
    max_scan_bytes: int
    require_complete_catalog: bool

    def validate_page(
        self,
        fetched_page: FetchedCatalogPage,
        *,
        requested_page_number: int,
        totals: CatalogScanTotals,
    ) -> CatalogScanTotals:
        """Return updated totals only when the page respects every scan bound."""
        page = fetched_page.page
        if len(page.data) > self.per_page:
            raise FeedFetchError(ANHOCH_LABEL, "PageCardinalityExceeded")
        if page.current_page != requested_page_number:
            raise FeedFetchError(ANHOCH_LABEL, "PageNumberMismatch")
        if self.require_complete_catalog and page.last_page > self.max_pages:
            raise FeedFetchError(ANHOCH_LABEL, "PageLimitExceeded")

        product_count = totals.product_count + len(page.data)
        if product_count > self.per_page * self.max_pages:
            raise FeedFetchError(ANHOCH_LABEL, "ProductLimitExceeded")

        response_bytes = totals.response_bytes + fetched_page.response_bytes
        if response_bytes > self.max_scan_bytes:
            raise FeedFetchError(ANHOCH_LABEL, "ScanResponseTooLarge")
        return CatalogScanTotals(
            product_count=product_count,
            response_bytes=response_bytes,
        )
