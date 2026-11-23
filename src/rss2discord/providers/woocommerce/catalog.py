"""Newest-window and complete-catalog WooCommerce Store API traversal."""

import time
from collections.abc import Callable
from datetime import UTC, datetime

from rss2discord.providers.woocommerce.bounds import (
    MAX_WOOCOMMERCE_CATALOG_SCAN_BYTES,
    MAX_WOOCOMMERCE_DISCOVERY_SCAN_BYTES,
    MAX_WOOCOMMERCE_PAGES,
    MAX_WOOCOMMERCE_PRODUCTS,
    MAX_WOOCOMMERCE_SCAN_SECONDS,
    WOOCOMMERCE_LABEL,
    WOOCOMMERCE_PAGE_SIZE,
    WOOCOMMERCE_WINDOW_SIZE,
    WooCommercePageRequest,
    WooCommerceShop,
)
from rss2discord.providers.woocommerce.http import (
    WooCommerceHttpClient,
    normalize_shop_url,
)
from rss2discord.providers.woocommerce.models import (
    WooCommerceDiscoveryProduct,
    WooCommerceProduct,
    first_party_product_url,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError


class WooCommerceCatalogClient:
    """Fetch latest or complete products from one public Store API."""

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[WooCommerceDiscoveryProduct, ...]:
        shop = normalize_shop_url(url)
        client = WooCommerceHttpClient(shop)
        category_id = (
            client.resolve_category_id(shop.category_slug)
            if shop.category_slug is not None
            else None
        )
        if is_shutdown_requested():
            raise FeedFetchInterruptedError
        fetched = client.fetch_products_page(
            WooCommercePageRequest(
                page=1,
                per_page=WOOCOMMERCE_WINDOW_SIZE,
                category_id=category_id,
                remaining_scan_bytes=MAX_WOOCOMMERCE_DISCOVERY_SCAN_BYTES,
            ),
        )
        expected = min(fetched.total, WOOCOMMERCE_WINDOW_SIZE)
        if len(fetched.products) != expected:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidCardinality")
        _require_first_party_products(fetched.products, shop)
        observed_at = datetime.now(UTC)
        return tuple(
            WooCommerceDiscoveryProduct(product, observed_at, shop.hostname)
            for product in reversed(fetched.products)
        )

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WooCommerceProduct, ...]:
        """Fetch the complete catalog, restarting every retry from page one."""
        shop = normalize_shop_url(url)
        return retry_policy.execute(
            lambda: self._scan_catalog(shop, is_shutdown_requested),
        )

    def _scan_catalog(
        self,
        shop: WooCommerceShop,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WooCommerceProduct, ...]:
        client = WooCommerceHttpClient(shop)
        category_id = (
            client.resolve_category_id(shop.category_slug)
            if shop.category_slug is not None
            else None
        )
        products: list[WooCommerceProduct] = []
        seen: dict[int, WooCommerceProduct] = {}
        scanned = 0
        remaining_bytes = MAX_WOOCOMMERCE_CATALOG_SCAN_BYTES
        expected_total: int | None = None
        expected_pages: int | None = None
        deadline = time.monotonic() + MAX_WOOCOMMERCE_SCAN_SECONDS
        for page in range(1, MAX_WOOCOMMERCE_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if time.monotonic() >= deadline:
                raise FeedFetchError(WOOCOMMERCE_LABEL, "ScanTimeLimitExceeded")
            fetched = client.fetch_products_page(
                WooCommercePageRequest(
                    page=page,
                    per_page=WOOCOMMERCE_PAGE_SIZE,
                    category_id=category_id,
                    remaining_scan_bytes=remaining_bytes,
                ),
            )
            remaining_bytes -= fetched.response_bytes
            _validate_catalog_size(fetched.total, fetched.total_pages)
            if expected_total is None:
                expected_total = fetched.total
                expected_pages = fetched.total_pages
            elif (
                fetched.total != expected_total or fetched.total_pages != expected_pages
            ):
                raise FeedFetchError(
                    WOOCOMMERCE_LABEL,
                    "PaginationDrift",
                    retryable=True,
                )
            if expected_total == 0:
                return ()
            expected = min(WOOCOMMERCE_PAGE_SIZE, expected_total - scanned)
            if len(fetched.products) != expected:
                raise FeedFetchError(
                    WOOCOMMERCE_LABEL,
                    "IncompleteCatalog",
                    retryable=True,
                )
            _require_first_party_products(fetched.products, shop)
            scanned += len(fetched.products)
            _append_unique_products(products, seen, fetched.products)
            if scanned >= expected_total:
                _require_complete_catalog(len(products), expected_total)
                return tuple(products)
        raise FeedFetchError(WOOCOMMERCE_LABEL, "PageLimitExceeded")


def _append_unique_products(
    products: list[WooCommerceProduct],
    seen: dict[int, WooCommerceProduct],
    page_products: tuple[WooCommerceProduct, ...],
) -> None:
    for product in page_products:
        if product.id in seen:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "DuplicateProductId")
        seen[product.id] = product
        products.append(product)


def _validate_catalog_size(total: int, pages: int) -> None:
    if total > MAX_WOOCOMMERCE_PRODUCTS:
        raise FeedFetchError(WOOCOMMERCE_LABEL, "ProductLimitExceeded")
    if pages > MAX_WOOCOMMERCE_PAGES:
        raise FeedFetchError(WOOCOMMERCE_LABEL, "PageLimitExceeded")


def _require_complete_catalog(unique_count: int, total: int) -> None:
    if unique_count != total:
        raise FeedFetchError(WOOCOMMERCE_LABEL, "IncompleteCatalog", retryable=True)


def _require_first_party_products(
    products: tuple[WooCommerceProduct, ...],
    shop: WooCommerceShop,
) -> None:
    for product in products:
        if first_party_product_url(product.permalink, shop.hostname) is None:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidProductUrl")
