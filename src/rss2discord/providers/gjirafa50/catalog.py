"""Newest-window and price-sharded Gjirafa50 catalog traversal."""

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from math import ceil
from typing import Final

from rss2discord.providers.gjirafa50.http import (
    Gjirafa50HttpClient,
    Gjirafa50PageRequest,
)
from rss2discord.providers.gjirafa50.models import (
    Gjirafa50CatalogPage,
    Gjirafa50PriceRange,
    Gjirafa50Product,
)
from rss2discord.providers.gjirafa50.parser import GJIRAFA50_LABEL
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError

GJIRAFA50_WINDOW_SIZE: Final = 30
GJIRAFA50_PAGE_SIZE: Final = 24
MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS: Final = 2_147_483_647 * 100 + 1
MAX_GJIRAFA50_SHARD_PRODUCTS: Final = 8_999
# The observed .com catalog has 137,124 products. At 24 per page, 150,000
# products plus 128 shards' probes/sentinels/reconciliation fit in 7,000
# requests. Retries share this work budget; they do not get a fresh allowance.
MAX_GJIRAFA50_PRODUCTS: Final = 150_000
MAX_GJIRAFA50_PAGES: Final = 7_000
MAX_GJIRAFA50_FETCHED_PRODUCTS: Final = MAX_GJIRAFA50_PAGES * GJIRAFA50_PAGE_SIZE
MAX_GJIRAFA50_SHARDS: Final = 128
MAX_GJIRAFA50_SCAN_BYTES: Final = 500 * 1024 * 1024
MAX_GJIRAFA50_SCAN_SECONDS: Final = 1_800

logger = logging.getLogger(__name__)


class _OperationBudget:
    """Share hard limits across every retry of one scheduled scan."""

    def __init__(self, is_shutdown_requested: Callable[[], bool]) -> None:
        self.is_shutdown_requested = is_shutdown_requested
        self.response_bytes = 0
        self.products = 0
        self.requests = 0
        self.shards = 0
        self.attempts = 0
        self.transfer_seconds = 0.0
        self.parse_seconds = 0.0
        self.deadline = time.monotonic() + MAX_GJIRAFA50_SCAN_SECONDS

    def before_request(self) -> None:
        self.check_active()
        # This counter tracks budget checks, including requests rejected below.
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
        """Bound cumulative parsed cards, including probes and retry attempts."""
        self.products += products
        if self.products > MAX_GJIRAFA50_FETCHED_PRODUCTS:
            raise FeedFetchError(GJIRAFA50_LABEL, "ProductWorkLimitExceeded")

    def consume_shard(self) -> None:
        self.shards += 1
        if self.shards > MAX_GJIRAFA50_SHARDS:
            raise FeedFetchError(GJIRAFA50_LABEL, "ShardLimitExceeded")

    def record_transfer_seconds(self, elapsed_seconds: float) -> None:
        self.transfer_seconds += max(elapsed_seconds, 0.0)

    def record_parse_seconds(self, elapsed_seconds: float) -> None:
        self.parse_seconds += max(elapsed_seconds, 0.0)

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
        self.phase = "initial"
        self.page_number = 1
        self.price_range: Gjirafa50PriceRange | None = None

    def fetch(
        self,
        page: int,
        price_range: Gjirafa50PriceRange | None = None,
        *,
        phase: str,
    ) -> Gjirafa50CatalogPage:
        self.phase = phase
        self.page_number = page
        self.price_range = price_range
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
                    raise FeedFetchError(
                        GJIRAFA50_LABEL,
                        "CatalogChanged",
                        retryable=True,
                    )
                expected_count = min(
                    GJIRAFA50_PAGE_SIZE,
                    max(page.total_hits - len(products), 0),
                )
                if len(page.products) != expected_count:
                    raise FeedFetchError(GJIRAFA50_LABEL, "InvalidCardinality")
                self._append_unique(
                    products,
                    seen,
                    page.products,
                    phase="discovery",
                    page_number=page_number,
                )
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
        budget.attempts += 1
        attempt = budget.attempts
        started_at = time.monotonic()
        requests_before = budget.requests
        bytes_before = budget.response_bytes
        products_before = budget.products
        shards_before = budget.shards
        transfer_before = budget.transfer_seconds
        parse_before = budget.parse_seconds
        root_total: int | None = None
        planned_leaves = 0
        completed_leaves = 0
        planned_enumeration_pages = 0
        products: dict[int, Gjirafa50Product] = {}
        outcome = "success"
        cause_type = "none"
        try:
            root_total = scan.fetch(1, phase="root_probe").total_hits
            if root_total > MAX_GJIRAFA50_PRODUCTS:
                logger.warning(
                    "Gjirafa50 catalog exceeds unique product capacity: "
                    "reported=%d limit=%d",
                    root_total,
                    MAX_GJIRAFA50_PRODUCTS,
                )
                raise FeedFetchError(  # noqa: TRY301
                    GJIRAFA50_LABEL,
                    "ProductLimitExceeded",
                )
            shards = self._build_shards(scan, root_total)
            planned_leaves = len(shards)
            planned_enumeration_pages = sum(
                ceil(total / GJIRAFA50_PAGE_SIZE) for _, total in shards
            )
            for price_range, shard_total in shards:
                self._scan_shard(scan, price_range, shard_total, root_total, products)
                completed_leaves += 1
            root_recount = scan.fetch(1, phase="root_recount").total_hits
            if root_recount != root_total:
                logger.warning(
                    "Gjirafa50 catalog root count changed: phase=root_recount "
                    "page=1 expected=%d actual=%d",
                    root_total,
                    root_recount,
                )
                raise FeedFetchError(  # noqa: TRY301
                    GJIRAFA50_LABEL,
                    "CatalogChanged",
                    retryable=True,
                )
            post_counts = tuple(
                scan.fetch(1, price_range, phase="leaf_recount").total_hits
                for price_range, _ in shards
            )
            expected_counts = tuple(total for _, total in shards)
            # This proves coverage only if root_total counts unique IDs and filter
            # results belong to the same stable root catalog; recounts catch some
            # drift but cannot establish an atomic snapshot.
            if post_counts != expected_counts or len(products) != root_total:
                raise FeedFetchError(  # noqa: TRY301
                    GJIRAFA50_LABEL,
                    "IncompleteCatalog",
                    retryable=True,
                )
            return tuple(products.values())
        except FeedFetchInterruptedError:
            outcome = "interrupted"
            cause_type = "FeedFetchInterruptedError"
            raise
        except FeedFetchError as error:
            outcome = "failed"
            cause = error.cause_type
            cause_type = (
                cause
                if len(cause) <= 100 and cause.isascii() and cause.isidentifier()
                else type(error).__name__
            )
            raise
        except BaseException as error:
            outcome = "failed"
            cause_type = type(error).__name__
            raise
        finally:
            elapsed = max(time.monotonic() - started_at, 0.0)
            details = (
                f"attempt={attempt} outcome={outcome} cause={cause_type} "
                f"root_total={root_total} unique_ids={len(products)} "
                f"planned_leaves={planned_leaves} completed_leaves={completed_leaves} "
                f"planned_enumeration_pages={planned_enumeration_pages} "
                f"phase={scan.phase} page={scan.page_number} "
                f"price_range={scan.price_range} elapsed_seconds={elapsed:.3f} "
                f"request_budget_checks_delta={budget.requests - requests_before} "
                f"request_budget_checks_total={budget.requests} "
                f"response_bytes_delta={budget.response_bytes - bytes_before} "
                f"response_bytes_total={budget.response_bytes} "
                f"parsed_cards_delta={budget.products - products_before} "
                f"parsed_cards_total={budget.products} "
                f"shards_delta={budget.shards - shards_before} "
                f"shards_total={budget.shards} "
                f"transfer_seconds_delta={budget.transfer_seconds - transfer_before:.3f} "
                f"transfer_seconds_total={budget.transfer_seconds:.3f} "
                f"parse_seconds_delta={budget.parse_seconds - parse_before:.3f} "
                f"parse_seconds_total={budget.parse_seconds:.3f}"
            )
            try:
                logger.info("Gjirafa50 price catalog attempt: %s", details)
            except Exception as logging_error:
                del logging_error

    def _build_shards(
        self,
        scan: _CatalogScan,
        root_total: int,
    ) -> tuple[tuple[Gjirafa50PriceRange, int], ...]:
        entire_range = Gjirafa50PriceRange(0, MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS)
        entire_total = scan.fetch(
            1,
            entire_range,
            phase="whole_range_probe",
        ).total_hits
        if entire_total != root_total:
            logger.warning(
                "Gjirafa50 catalog count mismatch: phase=whole_range "
                "range=%s parent_count=%d range_count=%d; catalog completeness "
                "is unverified",
                entire_range,
                root_total,
                entire_total,
            )
            raise FeedFetchError(
                GJIRAFA50_LABEL,
                "IncompletePriceRange",
                retryable=True,
            )
        queue = deque([(entire_range, entire_total)])
        shards: list[tuple[Gjirafa50PriceRange, int]] = []
        while queue:
            price_range, total = queue.popleft()
            if total <= MAX_GJIRAFA50_SHARD_PRODUCTS:
                scan.phase = "leaf_planning"
                scan.page_number = 1
                scan.price_range = price_range
                scan.budget.consume_shard()
                shards.append((price_range, total))
                continue
            if price_range.maximum_exclusive_cents - price_range.minimum_cents <= 2:
                raise FeedFetchError(GJIRAFA50_LABEL, "PriceBucketLimitExceeded")
            highest = price_range.maximum_exclusive_cents - 1
            midpoint = (price_range.minimum_cents + highest) // 2
            lower = Gjirafa50PriceRange(price_range.minimum_cents, midpoint + 1)
            upper = Gjirafa50PriceRange(
                midpoint,
                price_range.maximum_exclusive_cents,
            )
            lower_total = scan.fetch(1, lower, phase="split_probe").total_hits
            upper_total = scan.fetch(1, upper, phase="split_probe").total_hits
            queue.extend(((lower, lower_total), (upper, upper_total)))
            if len(queue) + len(shards) > MAX_GJIRAFA50_SHARDS:
                raise FeedFetchError(GJIRAFA50_LABEL, "ShardLimitExceeded")
        return tuple(sorted(shards, key=lambda item: item[0].minimum_cents))

    def _scan_shard(
        self,
        scan: _CatalogScan,
        price_range: Gjirafa50PriceRange,
        total: int,
        root_total: int,
        products: dict[int, Gjirafa50Product],
    ) -> None:
        pages = ceil(total / GJIRAFA50_PAGE_SIZE)
        for page_number in range(1, pages + 1):
            page = scan.fetch(
                page_number,
                price_range,
                phase="enumeration",
            )
            expected = min(
                GJIRAFA50_PAGE_SIZE,
                total - (page_number - 1) * GJIRAFA50_PAGE_SIZE,
            )
            if page.total_hits != total or page.total_pages != pages:
                logger.warning(
                    "Gjirafa50 shard count changed: phase=enumeration page=%d "
                    "price_range=%s expected_hits=%d actual_hits=%d "
                    "expected_pages=%d actual_pages=%d",
                    page_number,
                    price_range,
                    total,
                    page.total_hits,
                    pages,
                    page.total_pages,
                )
                raise FeedFetchError(GJIRAFA50_LABEL, "CatalogChanged", retryable=True)
            if len(page.products) > expected:
                logger.warning(
                    "Gjirafa50 overfull shard page: page=%d price_range=%s "
                    "expected=%d rendered=%d; catalog completeness is unverified",
                    page_number,
                    price_range,
                    expected,
                    len(page.products),
                )
                raise FeedFetchError(
                    GJIRAFA50_LABEL,
                    "IncompleteCatalog",
                    retryable=True,
                )
            if len(page.products) < expected:
                logger.warning(
                    "Gjirafa50 underfilled shard page: page=%d price_range=%s "
                    "expected=%d rendered=%d; global unique-ID reconciliation "
                    "is still required to verify completeness",
                    page_number,
                    price_range,
                    expected,
                    len(page.products),
                )
            for product in page.products:
                previous = products.get(product.id)
                if previous is not None:
                    if previous == product:
                        continue
                    logger.warning(
                        "Gjirafa50 conflicting duplicate product ID: "
                        "phase=enumeration page=%d product_id=%d price_range=%s; "
                        "catalog completeness is unverified",
                        page_number,
                        product.id,
                        price_range,
                    )
                    raise FeedFetchError(
                        GJIRAFA50_LABEL,
                        "DuplicateProductId",
                        retryable=True,
                    )
                if len(products) >= root_total:
                    logger.warning(
                        "Gjirafa50 unique product count exceeds root count: "
                        "phase=enumeration page=%d price_range=%s "
                        "root_total=%d unique_ids=%d",
                        page_number,
                        price_range,
                        root_total,
                        len(products) + 1,
                    )
                    raise FeedFetchError(
                        GJIRAFA50_LABEL,
                        "CatalogChanged",
                        retryable=True,
                    )
                if len(products) >= MAX_GJIRAFA50_PRODUCTS:
                    raise FeedFetchError(GJIRAFA50_LABEL, "ProductLimitExceeded")
                products[product.id] = product
        sentinel = scan.fetch(
            pages + 1,
            price_range,
            phase="terminal_check",
        )
        sentinel_count_reset = sentinel.total_hits == 0 and sentinel.total_pages == 0
        if sentinel.products or (
            not sentinel_count_reset
            and (sentinel.total_hits != total or sentinel.total_pages != pages)
        ):
            raise FeedFetchError(GJIRAFA50_LABEL, "InvalidTerminalPage", retryable=True)

    @staticmethod
    def _append_unique(
        products: list[Gjirafa50Product],
        seen: set[int],
        page_products: tuple[Gjirafa50Product, ...],
        *,
        phase: str,
        page_number: int,
        price_range: Gjirafa50PriceRange | None = None,
    ) -> None:
        for product in page_products:
            if product.id in seen:
                logger.warning(
                    "Gjirafa50 duplicate product ID: phase=%s page=%d "
                    "product_id=%d price_range=%s; catalog completeness is unverified",
                    phase,
                    page_number,
                    product.id,
                    price_range,
                )
                raise FeedFetchError(
                    GJIRAFA50_LABEL,
                    "DuplicateProductId",
                    retryable=True,
                )
            if len(seen) >= MAX_GJIRAFA50_PRODUCTS:
                raise FeedFetchError(GJIRAFA50_LABEL, "ProductLimitExceeded")
            seen.add(product.id)
            products.append(product)
