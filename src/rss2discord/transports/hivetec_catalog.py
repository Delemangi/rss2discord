"""Bounded latest-window and full-catalog Hivetec product scans."""

from collections.abc import Callable

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import hivetec_bounds as bounds
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.hivetec_bounds import (
    HIVETEC_CATALOG_PAGE_SIZE,
    HIVETEC_LABEL,
    HIVETEC_WINDOW_SIZE,
    MAX_HIVETEC_CATALOG_CATEGORIES,
    MAX_HIVETEC_CATALOG_IMAGES,
    MAX_HIVETEC_CATALOG_PAGES,
    MAX_HIVETEC_CATALOG_PRODUCTS,
    MAX_HIVETEC_DISCOVERY_SCAN_BYTES,
    MAX_HIVETEC_RESPONSE_BYTES,
    MAX_HIVETEC_SCAN_SECONDS,
    HivetecPageRequest,
)
from rss2discord.transports.hivetec_budget import HivetecScanBudget, HivetecScanLimits
from rss2discord.transports.hivetec_http import HivetecHttpClient
from rss2discord.transports.hivetec_models import (
    HivetecDiscoveryProduct,
    HivetecProduct,
)


class HivetecCatalogClient:
    """Fetch latest or complete Hivetec products from public WordPress APIs."""

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[HivetecDiscoveryProduct, ...]:
        budget = HivetecScanBudget.start(
            HivetecScanLimits(
                seconds=MAX_HIVETEC_SCAN_SECONDS,
                response_bytes=MAX_HIVETEC_DISCOVERY_SCAN_BYTES,
            ),
            is_shutdown_requested,
        )
        http_client = HivetecHttpClient(budget)
        api_urls = http_client.build_api_urls(url)
        request = HivetecPageRequest(
            page=1,
            per_page=HIVETEC_WINDOW_SIZE,
            max_response_bytes=MAX_HIVETEC_RESPONSE_BYTES,
        )
        fetched_products = http_client.fetch_products_page(api_urls.products, request)
        products = fetched_products.products
        dates = http_client.fetch_product_dates(api_urls.dates, request)
        expected_products = min(fetched_products.total, HIVETEC_WINDOW_SIZE)
        product_ids = tuple(product.id for product in products)
        date_ids = tuple(date.id for date in dates)
        if (
            len(products) != expected_products
            or len(dates) != expected_products
            or len(set(product_ids)) != len(product_ids)
            or len(set(date_ids)) != len(date_ids)
        ):
            raise FeedFetchError(HIVETEC_LABEL, "LatestWindowExceeded", retryable=True)
        if product_ids != date_ids:
            raise FeedFetchError(HIVETEC_LABEL, "DiscoveryDrift", retryable=True)
        return tuple(
            HivetecDiscoveryProduct(product=product, published_at=date.date_gmt)
            for product, date in reversed(tuple(zip(products, dates, strict=True)))
        )

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[HivetecProduct, ...]:
        """Fetch the complete catalog, restarting every retry from page one."""
        budget = HivetecScanBudget.start(
            HivetecScanLimits(
                seconds=MAX_HIVETEC_SCAN_SECONDS,
                response_bytes=bounds.MAX_HIVETEC_CATALOG_SCAN_BYTES,
            ),
            is_shutdown_requested,
        )
        http_client = HivetecHttpClient(budget)
        api_url = http_client.build_api_urls(url).products
        return retry_policy.execute(
            lambda: self._scan_catalog(api_url, http_client, is_shutdown_requested),
            retry_guard=budget.require_retry_delay,
        )

    def _scan_catalog(
        self,
        api_url: str,
        http_client: HivetecHttpClient,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[HivetecProduct, ...]:
        products: list[HivetecProduct] = []
        seen_products: dict[int, HivetecProduct] = {}
        expected_total: int | None = None
        expected_pages: int | None = None
        scanned_categories = 0
        scanned_images = 0
        scanned_products = 0
        for page in range(1, MAX_HIVETEC_CATALOG_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            fetched = http_client.fetch_products_page(
                api_url,
                HivetecPageRequest(
                    page=page,
                    per_page=HIVETEC_CATALOG_PAGE_SIZE,
                    max_response_bytes=MAX_HIVETEC_RESPONSE_BYTES,
                ),
            )
            if fetched.total > MAX_HIVETEC_CATALOG_PRODUCTS:
                raise FeedFetchError(HIVETEC_LABEL, "ProductLimitExceeded")
            if fetched.total_pages > MAX_HIVETEC_CATALOG_PAGES:
                raise FeedFetchError(HIVETEC_LABEL, "PageLimitExceeded")
            if expected_total is None:
                expected_total = fetched.total
                expected_pages = fetched.total_pages
            elif (
                fetched.total != expected_total or fetched.total_pages != expected_pages
            ):
                raise FeedFetchError(HIVETEC_LABEL, "PaginationDrift", retryable=True)
            if len(fetched.products) > HIVETEC_CATALOG_PAGE_SIZE:
                raise FeedFetchError(HIVETEC_LABEL, "PageCardinalityExceeded")
            scanned_categories += sum(
                len(product.categories) for product in fetched.products
            )
            scanned_images += sum(len(product.images) for product in fetched.products)
            if (
                scanned_categories > MAX_HIVETEC_CATALOG_CATEGORIES
                or scanned_images > MAX_HIVETEC_CATALOG_IMAGES
            ):
                raise FeedFetchError(HIVETEC_LABEL, "MetadataLimitExceeded")
            scanned_products += len(fetched.products)
            self._append_unique_products(products, seen_products, fetched.products)
            if expected_pages == 0 and expected_total == 0:
                return ()
            if expected_pages is not None and page >= expected_pages:
                if scanned_products != expected_total:
                    raise FeedFetchError(
                        HIVETEC_LABEL,
                        "IncompleteCatalog",
                        retryable=True,
                    )
                return tuple(products)
        raise FeedFetchError(HIVETEC_LABEL, "PageLimitExceeded")

    @staticmethod
    def _append_unique_products(
        products: list[HivetecProduct],
        seen_products: dict[int, HivetecProduct],
        page_products: tuple[HivetecProduct, ...],
    ) -> None:
        for product in page_products:
            existing = seen_products.get(product.id)
            if existing is None:
                seen_products[product.id] = product
                products.append(product)
                continue
            raise FeedFetchError(HIVETEC_LABEL, "DuplicateProductId")
