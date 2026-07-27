"""Bounded complete DDStore GraphQL catalog traversal."""

from collections.abc import Callable
from time import monotonic

from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.ddstore_budget import DDStoreScanBudget, DDStoreScanLimits
from rss2discord.transports.ddstore_http import (
    DDSTORE_LABEL,
    DDSTORE_PAGE_SIZE,
    CatalogPageRequest,
    DDStoreHttpClient,
)
from rss2discord.transports.ddstore_models import DDStoreProduct

MAX_DDSTORE_CATALOG_PRODUCTS = 20_000
MAX_DDSTORE_CATALOG_PAGES = 40
MAX_DDSTORE_RESPONSE_BYTES = 2_097_152
MAX_DDSTORE_CATALOG_SCAN_BYTES = 83_886_080
MAX_DDSTORE_CATALOG_SCAN_SECONDS = 300.0
DDSTORE_LATEST_WINDOW_SIZE = 30


class DDStoreCatalogClient:
    """Fetch the complete bounded DDStore catalog and its latest product window."""

    def __init__(self, monotonic_clock: Callable[[], float] = monotonic) -> None:
        self._monotonic = monotonic_clock

    def fetch_latest_products(
        self,
        url: str,
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[DDStoreProduct, ...]:
        """Fetch a complete validated catalog and return the latest 30 products."""
        budget = self._new_budget(is_shutdown_requested)
        products = self._scan_catalog(url, budget)
        return tuple(
            sorted(products, key=lambda product: (product.created_at, product.uid))[
                -DDSTORE_LATEST_WINDOW_SIZE:
            ],
        )

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[DDStoreProduct, ...]:
        """Fetch the complete catalog, restarting from page one after a retry."""
        budget = self._new_budget(is_shutdown_requested)
        return retry_policy.execute(
            lambda: self._scan_catalog(url, budget),
            retry_guard=budget.require_retry_delay,
        )

    def _new_budget(
        self,
        is_shutdown_requested: Callable[[], bool],
    ) -> DDStoreScanBudget:
        return DDStoreScanBudget(
            limits=DDStoreScanLimits(
                seconds=MAX_DDSTORE_CATALOG_SCAN_SECONDS,
                response_bytes=MAX_DDSTORE_CATALOG_SCAN_BYTES,
            ),
            monotonic=self._monotonic,
            is_shutdown_requested=is_shutdown_requested,
        )

    def _scan_catalog(
        self,
        url: str,
        budget: DDStoreScanBudget,
    ) -> tuple[DDStoreProduct, ...]:
        http_client = DDStoreHttpClient()
        try:
            graphql_url = http_client.build_graphql_url(url)
            products: list[DDStoreProduct] = []
            seen_products: dict[str, DDStoreProduct] = {}
            scanned_items = 0
            expected_metadata: tuple[int, int, int] | None = None
            for current_page in range(1, MAX_DDSTORE_CATALOG_PAGES + 1):
                budget.remaining_seconds()
                fetched_page = http_client.fetch_page(
                    graphql_url,
                    CatalogPageRequest(
                        current_page=current_page,
                        max_single_response_bytes=MAX_DDSTORE_RESPONSE_BYTES,
                    ),
                    budget,
                )
                page = fetched_page.catalog_response.products
                metadata = (
                    page.total_count,
                    page.page_info.page_size,
                    page.page_info.total_pages,
                )
                if expected_metadata is None:
                    self._validate_first_page_metadata(
                        page.page_info.current_page,
                        metadata,
                    )
                    if page.total_count == 0:
                        raise FeedFetchError(
                            DDSTORE_LABEL,
                            "EmptyCatalog",
                            retryable=True,
                        )
                    expected_metadata = metadata
                elif (
                    metadata != expected_metadata
                    or page.page_info.current_page != current_page
                ):
                    raise FeedFetchError(
                        DDSTORE_LABEL,
                        "CatalogMetadataDrift",
                        retryable=True,
                    )
                expected_page_items = min(
                    DDSTORE_PAGE_SIZE,
                    page.total_count - ((current_page - 1) * DDSTORE_PAGE_SIZE),
                )
                if len(page.items) != expected_page_items:
                    raise FeedFetchError(
                        DDSTORE_LABEL,
                        "PaginationDrift",
                        retryable=True,
                    )
                scanned_items += len(page.items)
                self._append_unique_products(products, seen_products, page.items)
                if current_page == page.page_info.total_pages:
                    if (
                        scanned_items != page.total_count
                        or len(products) != page.total_count
                    ):
                        raise FeedFetchError(
                            DDSTORE_LABEL,
                            "PaginationDrift",
                            retryable=True,
                        )
                    return tuple(products)
            raise FeedFetchError(DDSTORE_LABEL, "PageLimitExceeded")
        finally:
            http_client.close()

    @staticmethod
    def _validate_first_page_metadata(
        current_page: int,
        metadata: tuple[int, int, int],
    ) -> None:
        total_count, page_size, total_pages = metadata
        if total_count > MAX_DDSTORE_CATALOG_PRODUCTS:
            raise FeedFetchError(DDSTORE_LABEL, "ProductLimitExceeded")
        expected_pages = max(
            (total_count + DDSTORE_PAGE_SIZE - 1) // DDSTORE_PAGE_SIZE,
            1,
        )
        if (
            current_page != 1
            or page_size != DDSTORE_PAGE_SIZE
            or total_pages != expected_pages
            or total_pages > MAX_DDSTORE_CATALOG_PAGES
        ):
            raise FeedFetchError(DDSTORE_LABEL, "InvalidCatalogMetadata")

    @staticmethod
    def _append_unique_products(
        products: list[DDStoreProduct],
        seen_products: dict[str, DDStoreProduct],
        page_products: tuple[DDStoreProduct, ...],
    ) -> None:
        for product in page_products:
            if product.uid not in seen_products:
                seen_products[product.uid] = product
                products.append(product)
                continue
            raise FeedFetchError(
                DDSTORE_LABEL,
                "PaginationDrift",
                retryable=True,
            )
