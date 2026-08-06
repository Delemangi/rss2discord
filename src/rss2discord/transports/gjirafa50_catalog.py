"""Newest-window and price-sharded Gjirafa50 catalog traversal."""

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from math import ceil
from typing import Final

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.gjirafa50_http import (
    Gjirafa50HttpClient,
    Gjirafa50PageRequest,
)
from rss2discord.transports.gjirafa50_models import (
    Gjirafa50CatalogPage,
    Gjirafa50PriceRange,
    Gjirafa50Product,
)
from rss2discord.transports.gjirafa50_parser import GJIRAFA50_LABEL

GJIRAFA50_WINDOW_SIZE: Final = 30
GJIRAFA50_PAGE_SIZE: Final = 24
MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS: Final = 2_147_483_647 * 100 + 1
MAX_GJIRAFA50_SHARD_PRODUCTS: Final = 8_999
MAX_GJIRAFA50_PRODUCTS: Final = 100_000
MAX_GJIRAFA50_PAGES: Final = 5_000
MAX_GJIRAFA50_SHARDS: Final = 128
MAX_GJIRAFA50_SCAN_BYTES: Final = 500 * 1024 * 1024
MAX_GJIRAFA50_SCAN_SECONDS: Final = 1_800


class _OperationBudget:
    """Share hard limits across every retry of one scheduled scan."""

    def __init__(self, is_shutdown_requested: Callable[[], bool]) -> None:
        self.is_shutdown_requested = is_shutdown_requested
        self.response_bytes = 0
        self.products = 0
        self.requests = 0
        self.shards = 0
        self.deadline = time.monotonic() + MAX_GJIRAFA50_SCAN_SECONDS

    def before_request(self) -> None:
        self.check_active()
        self.requests += 1
        if self.requests > MAX_GJIRAFA50_PAGES:
            raise FeedFetchError(GJIRAFA50_LABEL, "PageLimitExceeded")

    def check_active(self) -> None:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if time.monotonic() >= self.deadline:
            raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")

    def consume_bytes(self, response_bytes: int) -> None:
        self.response_bytes += response_bytes
        if self.response_bytes > MAX_GJIRAFA50_SCAN_BYTES:
            raise FeedFetchError(GJIRAFA50_LABEL, "ScanResponseTooLarge")

    def consume_products(self, products: int) -> None:
        self.products += products
        if self.products > MAX_GJIRAFA50_PRODUCTS:
            raise FeedFetchError(GJIRAFA50_LABEL, "ProductLimitExceeded")

    def consume_shard(self) -> None:
        self.shards += 1
        if self.shards > MAX_GJIRAFA50_SHARDS:
            raise FeedFetchError(GJIRAFA50_LABEL, "ShardLimitExceeded")

    def guard_retry(self, delay: float) -> None:
        if time.monotonic() + delay >= self.deadline:
            raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")


class _CatalogScan:
    """Own mutable byte, request, and deadline state for one catalog attempt."""

    def __init__(
        self,
        root_url: str,
        budget: _OperationBudget,
        http: Gjirafa50HttpClient,
    ) -> None:
        self.root_url = root_url
        self.budget = budget
        self.http = http
        self.observed_at = datetime.now(UTC)

    def fetch(
        self,
        page: int,
        price_range: Gjirafa50PriceRange | None = None,
    ) -> Gjirafa50CatalogPage:
        fetched = self.http.fetch_page(
            self.root_url,
            Gjirafa50PageRequest(
                page=page,
                price_range=price_range,
                budget=self.budget,
            ),
            self.observed_at,
        )
        self.budget.consume_products(len(fetched.page.products))
        return fetched.page


class Gjirafa50CatalogClient:
    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[Gjirafa50Product, ...]:
        root_url = Gjirafa50HttpClient.normalize_root_url(url)
        observed_at = datetime.now(UTC)
        products: list[Gjirafa50Product] = []
        seen: set[int] = set()
        expected_total: int | None = None
        budget = _OperationBudget(is_shutdown_requested)
        budget.deadline = time.monotonic() + 60
        with Gjirafa50HttpClient() as http:
            for page_number in (1, 2):
                fetched = http.fetch_page(
                    root_url,
                    Gjirafa50PageRequest(
                        page=page_number,
                        order_by=16,
                        budget=budget,
                    ),
                    observed_at,
                )
                page = fetched.page
                if expected_total is None:
                    expected_total = page.total_hits
                elif page.total_hits != expected_total:
                    raise FeedFetchError(GJIRAFA50_LABEL, "CatalogChanged", retryable=True)
                expected_count = min(
                    GJIRAFA50_PAGE_SIZE,
                    max(page.total_hits - len(products), 0),
                )
                if len(page.products) != expected_count:
                    raise FeedFetchError(GJIRAFA50_LABEL, "InvalidCardinality")
                self._append_unique(products, seen, page.products)
                if len(products) >= min(page.total_hits, GJIRAFA50_WINDOW_SIZE):
                    break
        return tuple(reversed(products[:GJIRAFA50_WINDOW_SIZE]))

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Gjirafa50Product, ...]:
        root_url = Gjirafa50HttpClient.normalize_root_url(url)
        budget = _OperationBudget(is_shutdown_requested)
        with Gjirafa50HttpClient() as http:
            return retry_policy.execute(
                lambda: self._scan_catalog(root_url, budget, http),
                retry_guard=budget.guard_retry,
            )

    def _scan_catalog(
        self,
        root_url: str,
        budget: _OperationBudget,
        http: Gjirafa50HttpClient,
    ) -> tuple[Gjirafa50Product, ...]:
        scan = _CatalogScan(root_url, budget, http)
        root_total = scan.fetch(1).total_hits
        if root_total > MAX_GJIRAFA50_PRODUCTS:
            raise FeedFetchError(GJIRAFA50_LABEL, "ProductLimitExceeded")
        shards = self._build_shards(scan, root_total)
        products: list[Gjirafa50Product] = []
        seen: set[int] = set()
        for price_range, shard_total in shards:
            self._scan_shard(scan, price_range, shard_total, products, seen)
        if scan.fetch(1).total_hits != root_total:
            raise FeedFetchError(GJIRAFA50_LABEL, "CatalogChanged", retryable=True)
        post_counts = tuple(
            scan.fetch(1, price_range).total_hits for price_range, _ in shards
        )
        expected_counts = tuple(total for _, total in shards)
        if post_counts != expected_counts or len(products) != root_total:
            raise FeedFetchError(GJIRAFA50_LABEL, "IncompleteCatalog", retryable=True)
        return tuple(products)

    def _build_shards(
        self,
        scan: _CatalogScan,
        root_total: int,
    ) -> tuple[tuple[Gjirafa50PriceRange, int], ...]:
        entire_range = Gjirafa50PriceRange(0, MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS)
        entire_total = scan.fetch(1, entire_range).total_hits
        if entire_total != root_total:
            raise FeedFetchError(GJIRAFA50_LABEL, "IncompletePriceRange", retryable=True)
        queue = deque([(entire_range, entire_total)])
        shards: list[tuple[Gjirafa50PriceRange, int]] = []
        while queue:
            price_range, total = queue.popleft()
            if total <= MAX_GJIRAFA50_SHARD_PRODUCTS:
                scan.budget.consume_shard()
                shards.append((price_range, total))
                continue
            if (
                price_range.maximum_exclusive_cents - price_range.minimum_cents
                <= 1
            ):
                raise FeedFetchError(GJIRAFA50_LABEL, "PriceBucketLimitExceeded")
            midpoint = (
                price_range.minimum_cents + price_range.maximum_exclusive_cents
            ) // 2
            lower = Gjirafa50PriceRange(price_range.minimum_cents, midpoint)
            upper = Gjirafa50PriceRange(
                midpoint,
                price_range.maximum_exclusive_cents,
            )
            lower_total = scan.fetch(1, lower).total_hits
            upper_total = scan.fetch(1, upper).total_hits
            if lower_total + upper_total != total:
                raise FeedFetchError(GJIRAFA50_LABEL, "CatalogChanged", retryable=True)
            queue.extend(((lower, lower_total), (upper, upper_total)))
            if len(queue) + len(shards) > MAX_GJIRAFA50_SHARDS:
                raise FeedFetchError(GJIRAFA50_LABEL, "ShardLimitExceeded")
        return tuple(sorted(shards, key=lambda item: item[0].minimum_cents))

    def _scan_shard(
        self,
        scan: _CatalogScan,
        price_range: Gjirafa50PriceRange,
        total: int,
        products: list[Gjirafa50Product],
        seen: set[int],
    ) -> None:
        pages = ceil(total / GJIRAFA50_PAGE_SIZE)
        for page_number in range(1, pages + 1):
            page = scan.fetch(page_number, price_range)
            expected = min(
                GJIRAFA50_PAGE_SIZE,
                total - (page_number - 1) * GJIRAFA50_PAGE_SIZE,
            )
            if page.total_hits != total or page.total_pages != pages:
                raise FeedFetchError(GJIRAFA50_LABEL, "CatalogChanged", retryable=True)
            if len(page.products) != expected:
                raise FeedFetchError(GJIRAFA50_LABEL, "IncompleteCatalog", retryable=True)
            for product in page.products:
                price_cents = int(product.price * 100)
                if not (
                    price_range.minimum_cents
                    <= price_cents
                    < price_range.maximum_exclusive_cents
                ):
                    raise FeedFetchError(GJIRAFA50_LABEL, "PriceOutsideShard")
            self._append_unique(products, seen, page.products)
        sentinel = scan.fetch(pages + 1, price_range)
        if sentinel.total_hits != total or sentinel.total_pages != pages or sentinel.products:
            raise FeedFetchError(GJIRAFA50_LABEL, "InvalidTerminalPage", retryable=True)

    @staticmethod
    def _append_unique(
        products: list[Gjirafa50Product],
        seen: set[int],
        page_products: tuple[Gjirafa50Product, ...],
    ) -> None:
        for product in page_products:
            if product.id in seen:
                raise FeedFetchError(GJIRAFA50_LABEL, "DuplicateProductId", retryable=True)
            seen.add(product.id)
            products.append(product)
