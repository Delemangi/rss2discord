"""Newest-window and price-sharded Gjirafa50 catalog traversal."""

import logging
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from math import ceil
from threading import Lock, local
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
# The observed .com catalog has roughly 124,000 products. At 24 per page, 150,000
# products plus 128 shards' probes/sentinels/reconciliation fit in 7,000
# requests. Retries share this work budget; they do not get a fresh allowance.
MAX_GJIRAFA50_PRODUCTS: Final = 150_000
MAX_GJIRAFA50_PAGES: Final = 7_000
MAX_GJIRAFA50_FETCHED_PRODUCTS: Final = MAX_GJIRAFA50_PAGES * GJIRAFA50_PAGE_SIZE
MAX_GJIRAFA50_SHARDS: Final = 128
MAX_GJIRAFA50_SCAN_BYTES: Final = 2 * 1024 * 1024 * 1024
MAX_GJIRAFA50_SCAN_SECONDS: Final = 3_600
# A best-effort scan is only usable if it observed nearly all priced products.
# Skipped cards count toward coverage because they exist but cannot be monitored.
GJIRAFA50_MIN_COVERAGE_RATIO: Final = 0.90
# Page enumeration is network-latency bound. A small fixed worker pool raises
# throughput while the global host pacer keeps the request rate unchanged.
GJIRAFA50_SCAN_WORKERS: Final = 6
# Small shards stay sequential so fake-session tests and tiny scans avoid the
# executor and its per-thread HTTP sessions entirely.
GJIRAFA50_PARALLEL_PAGE_THRESHOLD: Final = 20
# Budget limits must abort the scan even while page-level fetch errors are
# tolerated; these causes are never treated as skippable catalog drift.
GJIRAFA50_TERMINAL_ERROR_CAUSES: Final = frozenset(
    {
        "PageLimitExceeded",
        "PriceBucketLimitExceeded",
        "ProductLimitExceeded",
        "ProductWorkLimitExceeded",
        "ScanResponseTooLarge",
        "ScanTimeLimitExceeded",
        "ShardLimitExceeded",
    },
)

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
        self._lock = Lock()

    def before_request(self) -> None:
        self.check_active()
        # This counter tracks budget checks, including requests rejected below.
        with self._lock:
            self.requests += 1
            if self.requests > MAX_GJIRAFA50_PAGES:
                raise FeedFetchError(GJIRAFA50_LABEL, "PageLimitExceeded")

    def check_active(self) -> None:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if time.monotonic() >= self.deadline:
            raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")

    def consume_bytes(self, response_bytes: int) -> None:
        with self._lock:
            self.response_bytes += response_bytes
            if self.response_bytes > MAX_GJIRAFA50_SCAN_BYTES:
                raise FeedFetchError(GJIRAFA50_LABEL, "ScanResponseTooLarge")

    def consume_products(self, products: int) -> None:
        """Bound cumulative parsed cards, including probes and retry attempts."""
        with self._lock:
            self.products += products
            if self.products > MAX_GJIRAFA50_FETCHED_PRODUCTS:
                raise FeedFetchError(GJIRAFA50_LABEL, "ProductWorkLimitExceeded")

    def consume_shard(self) -> None:
        with self._lock:
            self.shards += 1
            if self.shards > MAX_GJIRAFA50_SHARDS:
                raise FeedFetchError(GJIRAFA50_LABEL, "ShardLimitExceeded")

    def record_transfer_seconds(self, elapsed_seconds: float) -> None:
        with self._lock:
            self.transfer_seconds += max(elapsed_seconds, 0.0)

    def record_parse_seconds(self, elapsed_seconds: float) -> None:
        with self._lock:
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
        observed_at: datetime | None = None,
    ) -> None:
        self.root_url = root_url
        self.budget = budget
        self.http = http
        self.observed_at = datetime.now(UTC) if observed_at is None else observed_at
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
        self.budget.consume_products(
            len(fetched.page.products) + fetched.page.skipped_products,
        )
        return fetched.page


class _ScanWorkerPool:
    """Own a bounded executor and one HTTP session per worker thread."""

    def __init__(self, max_workers: int | None = None) -> None:
        workers = GJIRAFA50_SCAN_WORKERS if max_workers is None else max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="gjirafa50-scan",
        )
        self._local = local()
        self._clients: list[Gjirafa50HttpClient] = []
        self._lock = Lock()

    def submit(
        self,
        scan: _CatalogScan,
        page: int,
        price_range: Gjirafa50PriceRange,
    ) -> Future[Gjirafa50CatalogPage]:
        return self._executor.submit(self._fetch, scan, page, price_range)

    def _fetch(
        self,
        scan: _CatalogScan,
        page: int,
        price_range: Gjirafa50PriceRange,
    ) -> Gjirafa50CatalogPage:
        client = getattr(self._local, "client", None)
        if client is None:
            client = Gjirafa50HttpClient()
            self._local.client = client
            with self._lock:
                self._clients.append(client)
        # Each task owns phase/page/price_range, so workers never share a scan.
        worker_scan = _CatalogScan(
            scan.root_url,
            scan.budget,
            client,
            scan.observed_at,
        )
        return worker_scan.fetch(page, price_range, phase="enumeration")

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.close()


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
        seen_cards = 0
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
                    max(page.total_hits - seen_cards, 0),
                )
                observed_count = len(page.products) + page.skipped_products
                if observed_count != expected_count:
                    raise FeedFetchError(GJIRAFA50_LABEL, "InvalidCardinality")
                self._append_unique(
                    products,
                    seen,
                    page.products,
                    phase="discovery",
                    page_number=page_number,
                )
                seen_cards += observed_count
                if seen_cards >= min(page.total_hits, GJIRAFA50_WINDOW_SIZE):
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
        worker_pool = _ScanWorkerPool()
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
        priced_total: int | None = None
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
            entire_range = Gjirafa50PriceRange(
                0,
                MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS,
            )
            priced_total = scan.fetch(
                1,
                entire_range,
                phase="whole_range_probe",
            ).total_hits
            if priced_total > root_total:
                logger.warning(
                    "Gjirafa50 priced range exceeds root count: phase=whole_range "
                    "range=%s parent_count=%d range_count=%d",
                    entire_range,
                    root_total,
                    priced_total,
                )
                raise FeedFetchError(  # noqa: TRY301
                    GJIRAFA50_LABEL,
                    "IncompletePriceRange",
                    retryable=True,
                )
            if priced_total > MAX_GJIRAFA50_PRODUCTS:
                logger.warning(
                    "Gjirafa50 priced catalog exceeds unique product capacity: "
                    "reported=%d limit=%d",
                    priced_total,
                    MAX_GJIRAFA50_PRODUCTS,
                )
                raise FeedFetchError(  # noqa: TRY301
                    GJIRAFA50_LABEL,
                    "ProductLimitExceeded",
                )
            if priced_total < root_total:
                logger.warning(
                    "Gjirafa50 unpriced products excluded from scan: "
                    "root_total=%d priced_total=%d excluded=%d",
                    root_total,
                    priced_total,
                    root_total - priced_total,
                )
            shards = self._build_shards(scan, entire_range, priced_total)
            planned_leaves = len(shards)
            planned_enumeration_pages = sum(
                ceil(total / GJIRAFA50_PAGE_SIZE) for _, total in shards
            )
            skipped = 0
            for price_range, shard_total in shards:
                skipped += self._scan_shard(
                    scan,
                    price_range,
                    shard_total,
                    root_total,
                    products,
                    worker_pool=worker_pool,
                )
                completed_leaves += 1
            root_recount = scan.fetch(1, phase="root_recount").total_hits
            if root_recount != root_total:
                logger.warning(
                    "Gjirafa50 catalog root count changed: phase=root_recount "
                    "page=1 expected=%d actual=%d; continuing best-effort scan",
                    root_total,
                    root_recount,
                )
            for price_range, shard_total in shards:
                post_count = scan.fetch(
                    1,
                    price_range,
                    phase="leaf_recount",
                ).total_hits
                if post_count != shard_total:
                    logger.warning(
                        "Gjirafa50 leaf count changed: phase=leaf_recount page=1 "
                        "price_range=%s expected=%d actual=%d; continuing "
                        "best-effort scan",
                        price_range,
                        shard_total,
                        post_count,
                    )
            coverage = (
                1.0 if priced_total == 0 else (len(products) + skipped) / priced_total
            )
            if coverage < GJIRAFA50_MIN_COVERAGE_RATIO:
                logger.warning(
                    "Gjirafa50 catalog coverage below minimum: priced_total=%d "
                    "unique_ids=%d skipped=%d coverage=%.4f minimum=%.2f",
                    priced_total,
                    len(products),
                    skipped,
                    coverage,
                    GJIRAFA50_MIN_COVERAGE_RATIO,
                )
                raise FeedFetchError(  # noqa: TRY301
                    GJIRAFA50_LABEL,
                    "IncompleteCatalog",
                    retryable=True,
                )
            logger.info(
                "Gjirafa50 best-effort catalog coverage: root_total=%d "
                "priced_total=%d unique_ids=%d skipped=%d coverage=%.4f",
                root_total,
                priced_total,
                len(products),
                skipped,
                coverage,
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
            try:
                worker_pool.close()
            except Exception as close_error:
                del close_error
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
        entire_range: Gjirafa50PriceRange,
        priced_total: int,
    ) -> tuple[tuple[Gjirafa50PriceRange, int], ...]:
        queue = deque([(entire_range, priced_total)])
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
        *,
        worker_pool: _ScanWorkerPool | None = None,
    ) -> int:
        pages = ceil(total / GJIRAFA50_PAGE_SIZE)
        # Keep the attempt summary pointing at enumeration for this shard; each
        # parallel worker owns its own scan for phase/page/price_range.
        scan.phase = "enumeration"
        scan.page_number = 1
        scan.price_range = price_range
        if worker_pool is not None and pages >= GJIRAFA50_PARALLEL_PAGE_THRESHOLD:
            skipped = self._scan_shard_pages_parallel(
                scan,
                price_range,
                total=total,
                pages=pages,
                root_total=root_total,
                products=products,
                worker_pool=worker_pool,
            )
        else:
            skipped = self._scan_shard_pages_sequential(
                scan,
                price_range,
                total=total,
                pages=pages,
                root_total=root_total,
                products=products,
            )
        try:
            sentinel = scan.fetch(
                pages + 1,
                price_range,
                phase="terminal_check",
            )
        except FeedFetchError as error:
            self._log_shard_fetch_error(
                "terminal_check",
                error,
                pages + 1,
                price_range,
            )
            return skipped
        sentinel_count_reset = sentinel.total_hits == 0 and sentinel.total_pages == 0
        if sentinel.products or (
            not sentinel_count_reset
            and (sentinel.total_hits != total or sentinel.total_pages != pages)
        ):
            logger.warning(
                "Gjirafa50 sentinel page mismatch: page=%d price_range=%s "
                "expected_hits=%d actual_hits=%d expected_pages=%d actual_pages=%d "
                "rendered=%d; continuing best-effort scan",
                pages + 1,
                price_range,
                total,
                sentinel.total_hits,
                pages,
                sentinel.total_pages,
                len(sentinel.products),
            )
        return skipped

    def _scan_shard_pages_sequential(
        self,
        scan: _CatalogScan,
        price_range: Gjirafa50PriceRange,
        *,
        total: int,
        pages: int,
        root_total: int,
        products: dict[int, Gjirafa50Product],
    ) -> int:
        skipped = 0
        for page_number in range(1, pages + 1):
            try:
                page = scan.fetch(
                    page_number,
                    price_range,
                    phase="enumeration",
                )
            except FeedFetchError as error:
                self._log_shard_fetch_error(
                    "enumeration",
                    error,
                    page_number,
                    price_range,
                )
                continue
            skipped += self._process_shard_page(
                page,
                page_number,
                price_range,
                total=total,
                pages=pages,
                root_total=root_total,
                products=products,
            )
        return skipped

    def _scan_shard_pages_parallel(
        self,
        scan: _CatalogScan,
        price_range: Gjirafa50PriceRange,
        *,
        total: int,
        pages: int,
        root_total: int,
        products: dict[int, Gjirafa50Product],
        worker_pool: _ScanWorkerPool,
    ) -> int:
        futures = [
            worker_pool.submit(scan, page_number, price_range)
            for page_number in range(1, pages + 1)
        ]
        skipped = 0
        try:
            # Results are consumed in page order even when workers finish out of
            # order, so duplicate/conflict handling stays deterministic.
            for page_number, future in enumerate(futures, start=1):
                try:
                    page = future.result()
                except FeedFetchError as error:
                    self._log_shard_fetch_error(
                        "enumeration",
                        error,
                        page_number,
                        price_range,
                    )
                    continue
                skipped += self._process_shard_page(
                    page,
                    page_number,
                    price_range,
                    total=total,
                    pages=pages,
                    root_total=root_total,
                    products=products,
                )
        finally:
            for future in futures:
                future.cancel()
        return skipped

    def _process_shard_page(
        self,
        page: Gjirafa50CatalogPage,
        page_number: int,
        price_range: Gjirafa50PriceRange,
        *,
        total: int,
        pages: int,
        root_total: int,
        products: dict[int, Gjirafa50Product],
    ) -> int:
        expected = min(
            GJIRAFA50_PAGE_SIZE,
            total - (page_number - 1) * GJIRAFA50_PAGE_SIZE,
        )
        if page.total_hits != total or page.total_pages != pages:
            logger.warning(
                "Gjirafa50 shard count changed: phase=enumeration page=%d "
                "price_range=%s expected_hits=%d actual_hits=%d "
                "expected_pages=%d actual_pages=%d; continuing best-effort scan",
                page_number,
                price_range,
                total,
                page.total_hits,
                pages,
                page.total_pages,
            )
        observed = len(page.products) + page.skipped_products
        if observed != expected:
            logger.warning(
                "Gjirafa50 shard page cardinality changed: page=%d "
                "price_range=%s expected=%d observed=%d skipped=%d; "
                "continuing best-effort scan",
                page_number,
                price_range,
                expected,
                observed,
                page.skipped_products,
            )
        self._merge_shard_products(
            page.products,
            products,
            root_total,
            page_number,
            price_range,
        )
        return page.skipped_products

    @staticmethod
    def _log_shard_fetch_error(
        phase: str,
        error: FeedFetchError,
        page_number: int,
        price_range: Gjirafa50PriceRange,
    ) -> None:
        if error.cause_type in GJIRAFA50_TERMINAL_ERROR_CAUSES:
            raise error
        logger.warning(
            "Gjirafa50 shard page fetch failed: phase=%s page=%d price_range=%s "
            "cause=%s; continuing best-effort scan",
            phase,
            page_number,
            price_range,
            error.cause_type,
        )

    @staticmethod
    def _merge_shard_products(
        incoming: tuple[Gjirafa50Product, ...],
        products: dict[int, Gjirafa50Product],
        root_total: int,
        page_number: int,
        price_range: Gjirafa50PriceRange,
    ) -> None:
        for product in incoming:
            previous = products.get(product.id)
            if previous is not None:
                if previous != product:
                    logger.warning(
                        "Gjirafa50 conflicting duplicate product ID: "
                        "phase=enumeration page=%d product_id=%d price_range=%s; "
                        "keeping first observation",
                        page_number,
                        product.id,
                        price_range,
                    )
                continue
            if len(products) >= root_total:
                logger.warning(
                    "Gjirafa50 unique product count exceeds root count: "
                    "phase=enumeration page=%d price_range=%s "
                    "root_total=%d unique_ids=%d; continuing best-effort scan",
                    page_number,
                    price_range,
                    root_total,
                    len(products) + 1,
                )
            if len(products) >= MAX_GJIRAFA50_PRODUCTS:
                raise FeedFetchError(GJIRAFA50_LABEL, "ProductLimitExceeded")
            products[product.id] = product

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
                    "product_id=%d price_range=%s; keeping first observation",
                    phase,
                    page_number,
                    product.id,
                    price_range,
                )
                continue
            if len(seen) >= MAX_GJIRAFA50_PRODUCTS:
                raise FeedFetchError(GJIRAFA50_LABEL, "ProductLimitExceeded")
            seen.add(product.id)
            products.append(product)
