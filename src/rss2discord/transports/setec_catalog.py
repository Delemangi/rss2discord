"""Bounded complete and latest-window scans for Setec catalog products."""

from collections.abc import Callable

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import setec_catalog_bounds as bounds
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_CATALOG_PAGES,
    MAX_SETEC_CATALOG_PRODUCTS,
    MAX_SETEC_CATALOG_RESPONSE_BYTES,
    MAX_SETEC_LATEST_RESPONSE_BYTES,
    MAX_SETEC_REDIRECTS,
    SETEC_CATALOG_PAGE_SIZE,
    SETEC_LABEL,
    SETEC_WINDOW_SIZE,
    CatalogPageRequest,
)
from rss2discord.transports.setec_http import SetecHttpClient
from rss2discord.transports.setec_models import SetecProduct


class SetecCatalogClient:
    """Fetch complete or latest-window Setec products with bounded traversal."""

    def fetch_latest_products(self, url: str) -> tuple[SetecProduct, ...]:
        """Fetch the existing probe-based latest Setec product window."""
        http_client = SetecHttpClient()
        api_url = http_client.build_api_url(url)
        latest_scan_response_bytes = MAX_SETEC_LATEST_RESPONSE_BYTES * (
            MAX_SETEC_REDIRECTS + 1
        )
        probe = http_client.fetch_page(
            api_url,
            CatalogPageRequest(
                limit=1,
                offset=0,
                max_single_response_bytes=MAX_SETEC_LATEST_RESPONSE_BYTES,
                remaining_scan_response_bytes=latest_scan_response_bytes,
            ),
        ).catalog_response
        if probe.count == 0:
            return ()
        if probe.count <= 1:
            return probe.products
        latest = http_client.fetch_page(
            api_url,
            CatalogPageRequest(
                limit=SETEC_WINDOW_SIZE,
                offset=max(probe.count - SETEC_WINDOW_SIZE, 0),
                max_single_response_bytes=MAX_SETEC_LATEST_RESPONSE_BYTES,
                remaining_scan_response_bytes=latest_scan_response_bytes,
            ),
        ).catalog_response
        return latest.products

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        """Fetch the complete bounded catalog, restarting every retry from page one."""
        http_client = SetecHttpClient()
        api_url = http_client.build_api_url(url)
        return retry_policy.execute(
            lambda: self._scan_catalog(
                api_url,
                http_client,
                is_shutdown_requested,
            ),
        )

    def _scan_catalog(
        self,
        api_url: str,
        http_client: SetecHttpClient,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        products: list[SetecProduct] = []
        seen_products: dict[str, SetecProduct] = {}
        scan_response_bytes = 0
        scanned_products = 0
        for page_number in range(MAX_SETEC_CATALOG_PAGES):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if scan_response_bytes >= bounds.MAX_SETEC_CATALOG_SCAN_BYTES:
                raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
            fetched_page = http_client.fetch_page(
                api_url,
                CatalogPageRequest(
                    limit=SETEC_CATALOG_PAGE_SIZE,
                    offset=page_number * SETEC_CATALOG_PAGE_SIZE,
                    max_single_response_bytes=MAX_SETEC_CATALOG_RESPONSE_BYTES,
                    remaining_scan_response_bytes=(
                        bounds.MAX_SETEC_CATALOG_SCAN_BYTES - scan_response_bytes
                    ),
                ),
            )
            scan_response_bytes += fetched_page.page_response_bytes
            catalog_response = fetched_page.catalog_response
            if catalog_response.count > MAX_SETEC_CATALOG_PRODUCTS:
                raise FeedFetchError(SETEC_LABEL, "ProductLimitExceeded")
            if len(catalog_response.products) > SETEC_CATALOG_PAGE_SIZE:
                raise FeedFetchError(SETEC_LABEL, "PageCardinalityExceeded")
            scanned_products += len(catalog_response.products)
            if scanned_products > MAX_SETEC_CATALOG_PRODUCTS:
                raise FeedFetchError(SETEC_LABEL, "ProductLimitExceeded")
            self._append_unique_products(
                products,
                seen_products,
                catalog_response.products,
            )
            if not catalog_response.products:
                if scanned_products < catalog_response.count:
                    raise FeedFetchError(
                        SETEC_LABEL,
                        "IncompleteCatalog",
                        retryable=True,
                    )
                return tuple(products)
            if scanned_products >= catalog_response.count:
                return tuple(products)
        raise FeedFetchError(SETEC_LABEL, "PageLimitExceeded")

    @staticmethod
    def _append_unique_products(
        products: list[SetecProduct],
        seen_products: dict[str, SetecProduct],
        page_products: tuple[SetecProduct, ...],
    ) -> None:
        for product in page_products:
            existing_product = seen_products.get(product.id)
            if existing_product is None:
                seen_products[product.id] = product
                products.append(product)
                continue
            if existing_product != product:
                raise FeedFetchError(SETEC_LABEL, "DuplicateProductId")
