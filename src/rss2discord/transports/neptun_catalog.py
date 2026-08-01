"""Newest-window and complete-category Neptun catalog traversal."""

from collections.abc import Callable
from typing import Final

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.neptun_http import (
    NEPTUN_LABEL,
    NEPTUN_RESPONSE_BYTES,
    NeptunHttpClient,
    NeptunPageRequest,
)
from rss2discord.transports.neptun_models import NeptunProduct

NEPTUN_WINDOW_SIZE: Final = 30
NEPTUN_CATALOG_PAGE_SIZE: Final = 50
MAX_NEPTUN_CATALOG_PAGES: Final = 100
MAX_NEPTUN_CATALOG_PRODUCTS: Final = 5_000
MAX_NEPTUN_CATALOG_SCAN_BYTES: Final = 500 * 1024 * 1024


class NeptunCatalogClient:
    """Fetch only the configured category within fixed request and scan bounds."""

    def fetch_latest_products(self, url: str) -> tuple[NeptunProduct, ...]:
        http = NeptunHttpClient()
        category_url = http.normalize_category_url(url)
        model, category_bytes = http.fetch_category_model(
            category_url,
            remaining_scan_bytes=NEPTUN_RESPONSE_BYTES * 2,
        )
        fetched = http.fetch_products(
            category_url=category_url,
            category_id=model.category_id,
            request=NeptunPageRequest(
                page=1,
                page_size=NEPTUN_WINDOW_SIZE,
                sort=7,
                remaining_scan_bytes=NEPTUN_RESPONSE_BYTES * 2 - category_bytes,
            ),
        )
        products = fetched.response.batch.items
        total_items = fetched.response.batch.config.total_items
        if len(products) > NEPTUN_WINDOW_SIZE:
            raise FeedFetchError(NEPTUN_LABEL, "PageCardinalityExceeded")
        if len(products) != min(total_items, NEPTUN_WINDOW_SIZE):
            raise FeedFetchError(NEPTUN_LABEL, "InvalidCardinality")
        unique_products: list[NeptunProduct] = []
        self._append_unique(unique_products, {}, products)
        return tuple(reversed(unique_products))

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeptunProduct, ...]:
        category_url = NeptunHttpClient().normalize_category_url(url)
        return retry_policy.execute(
            lambda: self._scan_catalog(category_url, is_shutdown_requested),
        )

    def _scan_catalog(
        self,
        category_url: str,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeptunProduct, ...]:
        http = NeptunHttpClient()
        if is_shutdown_requested():
            raise FeedFetchInterruptedError
        model, scan_bytes = http.fetch_category_model(
            category_url,
            remaining_scan_bytes=MAX_NEPTUN_CATALOG_SCAN_BYTES,
        )
        products: list[NeptunProduct] = []
        seen: dict[int, NeptunProduct] = {}
        scanned_count = 0
        expected_total: int | None = None
        for page in range(1, MAX_NEPTUN_CATALOG_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            fetched = http.fetch_products(
                category_url=category_url,
                category_id=model.category_id,
                request=NeptunPageRequest(
                    page=page,
                    page_size=NEPTUN_CATALOG_PAGE_SIZE,
                    sort=model.sort,
                    remaining_scan_bytes=MAX_NEPTUN_CATALOG_SCAN_BYTES - scan_bytes,
                ),
            )
            scan_bytes += fetched.response_bytes
            batch = fetched.response.batch
            total = batch.config.total_items
            if total > MAX_NEPTUN_CATALOG_PRODUCTS:
                raise FeedFetchError(NEPTUN_LABEL, "ProductLimitExceeded")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise FeedFetchError(NEPTUN_LABEL, "CatalogChanged", retryable=True)
            if len(batch.items) > NEPTUN_CATALOG_PAGE_SIZE:
                raise FeedFetchError(NEPTUN_LABEL, "PageCardinalityExceeded")
            expected_page_items = min(
                NEPTUN_CATALOG_PAGE_SIZE,
                total - scanned_count,
            )
            if len(batch.items) != expected_page_items:
                raise FeedFetchError(
                    NEPTUN_LABEL,
                    "IncompleteCatalog",
                    retryable=True,
                )
            scanned_count += len(batch.items)
            if scanned_count > MAX_NEPTUN_CATALOG_PRODUCTS:
                raise FeedFetchError(NEPTUN_LABEL, "ProductLimitExceeded")
            self._append_unique(products, seen, batch.items)
            if scanned_count >= total:
                if len(products) != total:
                    raise FeedFetchError(
                        NEPTUN_LABEL,
                        "IncompleteCatalog",
                        retryable=True,
                    )
                return tuple(products)
        raise FeedFetchError(NEPTUN_LABEL, "PageLimitExceeded")

    @staticmethod
    def _append_unique(
        products: list[NeptunProduct],
        seen: dict[int, NeptunProduct],
        page_products: tuple[NeptunProduct, ...],
    ) -> None:
        for product in page_products:
            if product.id in seen:
                raise FeedFetchError(
                    NEPTUN_LABEL,
                    "DuplicateProductId",
                    retryable=True,
                )
            seen[product.id] = product
            products.append(product)
