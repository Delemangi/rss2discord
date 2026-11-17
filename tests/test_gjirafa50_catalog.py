import threading
import time
from datetime import datetime
from decimal import Decimal

import pytest

from rss2discord.providers.gjirafa50 import (
    catalog as gjirafa50_catalog,
)
from rss2discord.providers.gjirafa50 import (
    http as gjirafa50_http,
)
from rss2discord.providers.gjirafa50.catalog import (
    Gjirafa50CatalogClient,
    _CatalogScan,
    _OperationBudget,
    _ScanWorkerPool,
)
from rss2discord.providers.gjirafa50.http import (
    FetchedGjirafa50Page,
    Gjirafa50HttpClient,
    Gjirafa50PageRequest,
)
from rss2discord.providers.gjirafa50.models import (
    Gjirafa50CatalogPage,
    Gjirafa50PriceRange,
    Gjirafa50Product,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import FeedFetchError
from tests.gjirafa50_helpers import (
    ROOT_URL,
    RecordingGet,
    StubResponse,
    catalog_payload,
    no_wait_retry_policy,
)


def test_latest_products_reads_two_pages_and_returns_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    page_one = [(product_id, 1_000 + product_id) for product_id in range(30, 6, -1)]
    page_two = [(product_id, 1_000 + product_id) for product_id in range(6, 0, -1)]
    get = RecordingGet(
        [
            StubResponse(catalog_payload(30, page_one, total_pages=2)),
            StubResponse(catalog_payload(30, page_two, total_pages=2)),
        ],
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    # When
    products = Gjirafa50CatalogClient().fetch_latest_products(ROOT_URL)

    # Then
    assert [product.id for product in products] == list(range(1, 31))
    assert [params["pagenumber"] for params in get.params] == [1, 2]
    assert all(params["orderby"] == 16 for params in get.params)


def test_latest_products_stops_before_request_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([])
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    client = Gjirafa50CatalogClient()
    with pytest.raises(FeedFetchInterruptedError):
        client.fetch_latest_products(
            ROOT_URL,
            is_shutdown_requested=lambda: True,
        )

    assert get.params == []


def test_latest_products_logs_duplicate_id_and_keeps_first(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    duplicate_id = 8123
    get = RecordingGet(
        [StubResponse(catalog_payload(2, [(duplicate_id, 1), (duplicate_id, 1)]))],
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    products = Gjirafa50CatalogClient().fetch_latest_products(ROOT_URL)

    assert [product.id for product in products] == [duplicate_id]
    assert len(get.params) == 1
    assert "phase=discovery page=1 product_id=8123 price_range=None" in caplog.text
    assert "keeping first observation" in caplog.text
    assert "Product 8123" not in caplog.text
    assert "/product-8123" not in caplog.text


def test_latest_products_counts_skipped_cards_toward_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = catalog_payload(2, [(1, 100), (2, 100)]).replace(
        b'data-productid=\\"2\\" data-discountedprice=\\"100,00\\"',
        b'data-productid=\\"2\\" data-discountedprice=\\"1,505\\"',
    )
    get = RecordingGet([StubResponse(payload)])
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    products = Gjirafa50CatalogClient().fetch_latest_products(ROOT_URL)

    assert [product.id for product in products] == [1]
    assert len(get.params) == 1


def test_catalog_logs_conflicting_duplicate_and_keeps_first(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    duplicate_id = 8123
    get = RecordingGet(
        [
            StubResponse(
                catalog_payload(
                    2,
                    [
                        (duplicate_id, Decimal("0.01")),
                        (duplicate_id, Decimal("0.02")),
                    ],
                ),
            ),
            StubResponse(catalog_payload(2, [], total_pages=1)),
        ],
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    collected: dict[int, Gjirafa50Product] = {}
    with Gjirafa50HttpClient(get) as http:
        scan = _CatalogScan(
            ROOT_URL,
            _OperationBudget(lambda: False),
            http,
        )
        Gjirafa50CatalogClient()._scan_shard(
            scan,
            Gjirafa50PriceRange(0, 2),
            2,
            2,
            collected,
        )

    assert list(collected) == [duplicate_id]
    assert collected[duplicate_id].price == Decimal("0.01")
    assert "phase=enumeration page=1 product_id=8123 price_range=" in caplog.text
    assert "keeping first observation" in caplog.text
    assert "Product 8123" not in caplog.text
    assert "/product-8123" not in caplog.text


def test_price_range_renders_exact_cent_boundaries_for_storefront_filter() -> None:
    price_range = Gjirafa50PriceRange(1_296_850, 1_296_900)

    assert str(price_range) == "12968,50-12968,99"


def test_catalog_recursively_shards_prices_and_reconciles_all_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 3)
    monkeypatch.setattr(
        gjirafa50_catalog,
        "MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS",
        4,
    )
    responses = [
        catalog_payload(4, [(4, Decimal("0.03"))]),
        catalog_payload(4, [(4, Decimal("0.03"))]),
        catalog_payload(2, [(1, Decimal(0)), (2, Decimal("0.01"))]),
        catalog_payload(
            3,
            [(2, Decimal("0.01")), (3, Decimal("0.02")), (4, Decimal("0.03"))],
        ),
        catalog_payload(2, [(1, Decimal(0)), (2, Decimal("0.01"))]),
        catalog_payload(2, [], total_pages=1),
        catalog_payload(
            3,
            [(2, Decimal("0.01")), (3, Decimal("0.02")), (4, Decimal("0.03"))],
        ),
        catalog_payload(3, [], total_pages=1),
        catalog_payload(4, [(4, Decimal("0.03"))]),
        catalog_payload(2, [(1, Decimal(0)), (2, Decimal("0.01"))]),
        catalog_payload(
            3,
            [(2, Decimal("0.01")), (3, Decimal("0.02")), (4, Decimal("0.03"))],
        ),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    # When
    products = Gjirafa50CatalogClient().fetch_catalog(
        ROOT_URL,
        retry_policy=no_wait_retry_policy(),
        is_shutdown_requested=lambda: False,
    )

    # Then
    assert [product.id for product in products] == [1, 2, 3, 4]
    assert [product.price for product in products] == [
        Decimal(0),
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.03"),
    ]
    requested_ranges = [params.get("price") for params in get.params]
    assert requested_ranges.count("0-0,01") == 4
    assert requested_ranges.count("0,01-0,03") == 4


def test_catalog_fails_closed_when_shard_counts_do_not_cover_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 3)
    monkeypatch.setattr(
        gjirafa50_catalog,
        "MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS",
        4,
    )
    lower = [(1, Decimal(0)), (2, Decimal("0.01"))]
    upper_missing_id = [(2, Decimal("0.01")), (3, Decimal("0.02"))]
    get = RecordingGet(
        [
            StubResponse(catalog_payload(4, [(4, Decimal("0.03"))])),
            StubResponse(catalog_payload(4, [(4, Decimal("0.03"))])),
            StubResponse(catalog_payload(2, lower)),
            StubResponse(catalog_payload(2, upper_missing_id)),
            StubResponse(catalog_payload(2, lower)),
            StubResponse(catalog_payload(2, [], total_pages=1)),
            StubResponse(catalog_payload(2, upper_missing_id)),
            StubResponse(catalog_payload(2, [], total_pages=1)),
            StubResponse(catalog_payload(4, [(4, Decimal("0.03"))])),
            StubResponse(catalog_payload(2, lower)),
            StubResponse(catalog_payload(2, upper_missing_id)),
        ]
        * 3,
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    # When / Then
    client = Gjirafa50CatalogClient()
    fetch_retry_policy = no_wait_retry_policy()
    with pytest.raises(FeedFetchError, match="IncompleteCatalog"):
        client.fetch_catalog(
            ROOT_URL,
            retry_policy=fetch_retry_policy,
            is_shutdown_requested=lambda: False,
        )


def test_catalog_logs_leaf_count_change_and_completes_scan(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 3)
    monkeypatch.setattr(
        gjirafa50_catalog,
        "MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS",
        4,
    )
    products = [
        (1, Decimal(0)),
        (2, Decimal("0.01")),
        (3, Decimal("0.02")),
        (4, Decimal("0.03")),
    ]
    responses = [
        catalog_payload(4, products[:1]),
        catalog_payload(4, products[:1]),
        catalog_payload(2, products[:2]),
        catalog_payload(3, [products[1], *products[2:]]),
        catalog_payload(2, products[:2]),
        catalog_payload(2, [], total_pages=1),
        catalog_payload(3, [products[1], *products[2:]]),
        catalog_payload(3, [], total_pages=1),
        catalog_payload(4, products[:1]),
        catalog_payload(2, products[:2]),
        catalog_payload(4, products),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    products_result = Gjirafa50CatalogClient().fetch_catalog(
        ROOT_URL,
        retry_policy=no_wait_retry_policy(),
        is_shutdown_requested=lambda: False,
    )

    assert {product.id for product in products_result} == {1, 2, 3, 4}
    assert "phase=leaf_recount page=1 price_range=0,01-0,03 expected=3 actual=4" in (
        caplog.text
    )
    assert "continuing best-effort scan" in caplog.text


def test_catalog_accepts_priced_total_below_root_total(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = [
        catalog_payload(3, []),
        catalog_payload(2, []),
        catalog_payload(2, [(1, 100), (2, 200)]),
        catalog_payload(2, [], total_pages=1),
        catalog_payload(3, []),
        catalog_payload(2, []),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])

    client = Gjirafa50CatalogClient()
    with Gjirafa50HttpClient(get) as http:
        products = client._scan_catalog(
            ROOT_URL,
            _OperationBudget(lambda: False),
            http,
        )

    assert {product.id for product in products} == {1, 2}
    assert "unpriced products excluded from scan" in caplog.text
    assert "root_total=3 priced_total=2 excluded=1" in caplog.text


def test_catalog_tolerates_small_count_inversion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING")
    responses = [
        catalog_payload(4, []),
        catalog_payload(5, []),
        catalog_payload(5, [(1, 100), (2, 200), (3, 300), (4, 400), (5, 500)]),
        catalog_payload(5, [], total_pages=1),
        catalog_payload(4, []),
        catalog_payload(5, []),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])

    client = Gjirafa50CatalogClient()
    with Gjirafa50HttpClient(get) as http:
        products = client._scan_catalog(
            ROOT_URL,
            _OperationBudget(lambda: False),
            http,
        )

    assert {product.id for product in products} == {1, 2, 3, 4, 5}
    assert "inversion=1 tolerance=100" in caplog.text
    assert "unique product count exceeds root count" not in caplog.text


def test_catalog_counts_skipped_cards_toward_coverage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    enumeration = catalog_payload(2, [(1, 100), (2, 100)]).replace(
        b'data-productid=\\"2\\" data-discountedprice=\\"100,00\\"',
        b'data-productid=\\"2\\" data-discountedprice=\\"1,505\\"',
    )
    responses = [
        catalog_payload(2, []),
        catalog_payload(2, []),
        enumeration,
        catalog_payload(2, [], total_pages=1),
        catalog_payload(2, []),
        catalog_payload(2, []),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])

    client = Gjirafa50CatalogClient()
    with Gjirafa50HttpClient(get) as http:
        products = client._scan_catalog(
            ROOT_URL,
            _OperationBudget(lambda: False),
            http,
        )

    assert [product.id for product in products] == [1]
    assert "skipped=1 coverage=1.0000" in caplog.text


def test_scan_shard_parallelizes_pages_and_preserves_page_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "GJIRAFA50_PARALLEL_PAGE_THRESHOLD", 2)
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: RecordingGet([]))
    total = 240
    pages = 10
    thread_ids: set[int] = set()
    thread_lock = threading.Lock()

    def fetch_page(
        _self: Gjirafa50HttpClient,
        _root_url: str,
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        with thread_lock:
            thread_ids.add(threading.get_ident())
        request.budget.before_request()
        request.budget.consume_bytes(1)
        # Make early pages slower so completion order differs from page order.
        time.sleep((pages - request.page + 1) * 0.002)
        if request.page <= pages:
            first_id = (request.page - 1) * 24 + 1
            products = tuple(
                Gjirafa50Product(
                    product_id,
                    f"Product {product_id}",
                    f"{ROOT_URL}product-{product_id}",
                    None,
                    Decimal(100),
                    "MKD",
                    "100,00 MKD.",
                    observed_at,
                )
                for product_id in range(first_id, first_id + 24)
            )
        else:
            products = ()
        return FetchedGjirafa50Page(
            Gjirafa50CatalogPage(total, pages, products),
            1,
        )

    monkeypatch.setattr(Gjirafa50HttpClient, "fetch_page", fetch_page)
    worker_pool = _ScanWorkerPool(max_workers=4)
    try:
        with Gjirafa50HttpClient(RecordingGet([])) as http:
            scan = _CatalogScan(
                ROOT_URL,
                _OperationBudget(lambda: False),
                http,
            )
            collected: dict[int, Gjirafa50Product] = {}
            skipped = Gjirafa50CatalogClient()._scan_shard(
                scan,
                Gjirafa50PriceRange(0, 1_000),
                total,
                total,
                collected,
                worker_pool=worker_pool,
            )
    finally:
        worker_pool.close()

    assert skipped == 0
    assert list(collected) == list(range(1, total + 1))
    assert len(thread_ids) > 1


def test_scan_shard_parallel_terminal_error_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "GJIRAFA50_PARALLEL_PAGE_THRESHOLD", 2)
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: RecordingGet([]))

    def fetch_page(
        _self: Gjirafa50HttpClient,
        _root_url: str,
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        del observed_at
        if request.page == 1:
            raise FeedFetchError("Gjirafa50", "ScanTimeLimitExceeded")
        time.sleep(0.01)
        return FetchedGjirafa50Page(Gjirafa50CatalogPage(240, 10, ()), 1)

    monkeypatch.setattr(Gjirafa50HttpClient, "fetch_page", fetch_page)
    worker_pool = _ScanWorkerPool(max_workers=4)
    try:
        with Gjirafa50HttpClient(RecordingGet([])) as http:
            scan = _CatalogScan(
                ROOT_URL,
                _OperationBudget(lambda: False),
                http,
            )
            with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
                Gjirafa50CatalogClient()._scan_shard(
                    scan,
                    Gjirafa50PriceRange(0, 1_000),
                    240,
                    240,
                    {},
                    worker_pool=worker_pool,
                )
    finally:
        worker_pool.close()


def _large_catalog_responses(failing_pages: set[int]) -> list[StubResponse]:
    total = 240
    pages = total // 24
    responses = [
        StubResponse(catalog_payload(total, [])),
        StubResponse(catalog_payload(total, [])),
    ]
    for page_number in range(1, pages + 1):
        if page_number in failing_pages:
            responses.append(StubResponse(b"temporary", status_code=503))
            continue
        first_id = (page_number - 1) * 24 + 1
        responses.append(
            StubResponse(
                catalog_payload(
                    total,
                    [
                        (product_id, product_id)
                        for product_id in range(first_id, first_id + 24)
                    ],
                    total_pages=pages,
                ),
            ),
        )
    responses.extend(
        [
            StubResponse(catalog_payload(total, [], total_pages=pages)),
            StubResponse(catalog_payload(total, [])),
            StubResponse(catalog_payload(total, [])),
        ],
    )
    return responses


def test_catalog_skips_failing_page_and_returns_ninety_percent_coverage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    get = RecordingGet(_large_catalog_responses({1}))

    client = Gjirafa50CatalogClient()
    with Gjirafa50HttpClient(get) as http:
        products = client._scan_catalog(
            ROOT_URL,
            _OperationBudget(lambda: False),
            http,
        )

    assert len(products) == 216
    assert 1 not in {product.id for product in products}
    assert "shard page fetch failed" in caplog.text
    assert "cause=HTTPError" in caplog.text
    assert "coverage=0.9000" in caplog.text


def test_catalog_raises_incomplete_catalog_below_ninety_percent_coverage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    get = RecordingGet(_large_catalog_responses({1, 2}))

    client = Gjirafa50CatalogClient()
    with (
        Gjirafa50HttpClient(get) as http,
        pytest.raises(FeedFetchError, match="IncompleteCatalog") as error,
    ):
        client._scan_catalog(
            ROOT_URL,
            _OperationBudget(lambda: False),
            http,
        )

    assert error.value.retryable is True
    assert "coverage=0.8000 minimum=0.90" in caplog.text


def test_root_recount_change_logs_numeric_catalog_diagnostic_and_attempt_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    product = [(1, Decimal(100))]
    get = RecordingGet(
        [
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, [], total_pages=1)),
            StubResponse(catalog_payload(2, product)),
            StubResponse(catalog_payload(1, product)),
        ],
    )

    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with Gjirafa50HttpClient(get) as http:
        products = client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert [product.id for product in products] == [1]
    assert "phase=root_recount page=1 expected=1 actual=2" in caplog.text
    assert "attempt=1 outcome=success cause=none root_total=1" in caplog.text
    assert "phase=root_recount page=1" in caplog.text
    assert "https://" not in caplog.text
    assert caplog.text.count("Gjirafa50 price catalog attempt:") == 1


def test_enumeration_hits_or_pages_change_logs_numeric_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    get = RecordingGet(
        [
            StubResponse(catalog_payload(1, [(1, 100)])),
            StubResponse(catalog_payload(1, [(1, 100)])),
            StubResponse(catalog_payload(2, [(1, 100)], total_pages=2)),
            StubResponse(catalog_payload(1, [], total_pages=1)),
            StubResponse(catalog_payload(1, [(1, 100)])),
            StubResponse(catalog_payload(1, [(1, 100)])),
        ],
    )

    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with Gjirafa50HttpClient(get) as http:
        products = client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert [product.id for product in products] == [1]
    assert (
        "phase=enumeration page=1 price_range=0-2147483647 "
        "expected_hits=1 actual_hits=2 expected_pages=1 actual_pages=2"
    ) in caplog.text
    assert "continuing best-effort scan" in caplog.text
    assert "https://" not in caplog.text


def test_excess_unique_ids_log_numeric_catalog_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = [
        catalog_payload(2, []),
        catalog_payload(2, []),
        catalog_payload(
            2,
            [(1, Decimal(0)), (2, Decimal("0.01")), (3, Decimal("0.02"))],
        ),
        catalog_payload(2, [], total_pages=1),
        catalog_payload(2, []),
        catalog_payload(2, []),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])

    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with Gjirafa50HttpClient(get) as http:
        products = client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert {product.id for product in products} == {1, 2, 3}
    assert "root_total=2 unique_ids=3" in caplog.text
    assert "continuing best-effort scan" in caplog.text
    assert "https://" not in caplog.text


def test_retry_attempt_summary_reports_fresh_inventory_and_delta_counters(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level("INFO")
    product = [(1, Decimal(100))]
    get = RecordingGet(
        [
            # Attempt 1 fails at the priced-range probe (retryable).
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1001, product)),
            # Attempt 2 succeeds.
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, [], total_pages=1)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
        ],
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    products = Gjirafa50CatalogClient().fetch_catalog(
        ROOT_URL,
        retry_policy=no_wait_retry_policy(),
        is_shutdown_requested=lambda: False,
    )

    summaries = [
        record.getMessage()
        for record in caplog.records
        if "Gjirafa50 price catalog attempt:" in record.getMessage()
    ]
    assert len(products) == 1
    assert len(summaries) == 2
    assert (
        "attempt=1 outcome=failed cause=IncompletePriceRange root_total=1 unique_ids=0"
        in summaries[0]
    )
    assert "request_budget_checks_delta=2 request_budget_checks_total=2" in summaries[0]
    assert "parsed_cards_delta=2 parsed_cards_total=2" in summaries[0]
    assert (
        "attempt=2 outcome=success cause=none root_total=1 unique_ids=1" in summaries[1]
    )
    assert "request_budget_checks_delta=6 request_budget_checks_total=8" in summaries[1]
    assert "parsed_cards_delta=5 parsed_cards_total=7" in summaries[1]
    assert (
        "planned_leaves=1 completed_leaves=1 planned_enumeration_pages=1"
        in summaries[1]
    )


def test_interrupted_price_scan_logs_one_summary_before_any_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    get = RecordingGet([])

    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: True)
    with (
        Gjirafa50HttpClient(get) as http,
        pytest.raises(FeedFetchInterruptedError),
    ):
        client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    summaries = [
        record.getMessage()
        for record in caplog.records
        if "Gjirafa50 price catalog attempt:" in record.getMessage()
    ]
    assert get.params == []
    assert len(summaries) == 1
    assert (
        "outcome=interrupted cause=FeedFetchInterruptedError root_total=None"
        in summaries[0]
    )
    assert "phase=root_probe page=1 price_range=None" in summaries[0]
    assert "request_budget_checks_delta=0 request_budget_checks_total=0" in summaries[0]


def test_attempt_summary_logs_exception_class_without_sensitive_message(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level("INFO")
    sensitive = "https://private.example/item raw-response=secret"
    original = RuntimeError(sensitive)

    def fail_page(*_args: object, **_kwargs: object) -> None:
        raise original

    monkeypatch.setattr(Gjirafa50HttpClient, "fetch_page", fail_page)
    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with (
        Gjirafa50HttpClient(RecordingGet([])) as http,
        pytest.raises(RuntimeError, match=r"private\.example") as error,
    ):
        client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert error.value is original
    assert "cause=RuntimeError" in caplog.text
    assert sensitive not in caplog.text


def test_success_inside_caller_exception_handler_logs_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    product = [(1, Decimal(100))]
    get = RecordingGet(
        [
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, [], total_pages=1)),
            StubResponse(catalog_payload(1, product)),
            StubResponse(catalog_payload(1, product)),
        ],
    )
    caller_error = RuntimeError("https://caller.example/private")

    try:
        raise caller_error
    except RuntimeError:
        with Gjirafa50HttpClient(get) as http:
            products = Gjirafa50CatalogClient()._scan_catalog(
                ROOT_URL,
                _OperationBudget(lambda: False),
                http,
            )

    summary = next(
        record.getMessage()
        for record in caplog.records
        if "Gjirafa50 price catalog attempt:" in record.getMessage()
    )
    assert len(products) == 1
    assert "outcome=success cause=none root_total=1 unique_ids=1" in summary
    assert "caller.example" not in summary


def test_unsafe_feed_fetch_cause_logs_only_safe_exception_class(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level("INFO")
    sensitive = "https://private.example/item raw-response=secret"
    original = FeedFetchError("Gjirafa50", sensitive, retryable=True)

    def fail_page(*_args: object, **_kwargs: object) -> None:
        raise original

    monkeypatch.setattr(Gjirafa50HttpClient, "fetch_page", fail_page)
    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with (
        Gjirafa50HttpClient(RecordingGet([])) as http,
        pytest.raises(FeedFetchError) as error,
    ):
        client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert error.value is original
    assert "cause=FeedFetchError" in caplog.text
    assert sensitive not in caplog.text


def test_catalog_request_budget_is_shared_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_PAGES", 3)
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 1)
    monkeypatch.setattr(
        gjirafa50_catalog,
        "MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS",
        4,
    )
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [(2, Decimal("0.01"))])),
            StubResponse(catalog_payload(2, [(2, Decimal("0.01"))])),
            StubResponse(b"retry", status_code=503),
        ],
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)
    retry_errors: list[FeedFetchError] = []
    retry_policy = FetchRetryPolicy(
        sleep=lambda _delay: True,
        on_retry=lambda error, _delay: retry_errors.append(error),
    )

    client = Gjirafa50CatalogClient()
    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        client.fetch_catalog(
            ROOT_URL,
            retry_policy=retry_policy,
            is_shutdown_requested=lambda: False,
        )

    assert len(retry_errors) == 1
    assert retry_errors[0].status_code == 503
    assert len(get.params) == 3


def test_catalog_product_budget_accumulates_across_retry_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_FETCHED_PRODUCTS", 3)
    budget = _OperationBudget(lambda: False)

    budget.consume_products(2)

    with pytest.raises(FeedFetchError, match="ProductWorkLimitExceeded"):
        budget.consume_products(2)


def test_catalog_shard_budget_accumulates_across_retry_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARDS", 1)
    budget = _OperationBudget(lambda: False)

    budget.consume_shard()

    with pytest.raises(FeedFetchError, match="ShardLimitExceeded"):
        budget.consume_shard()


def test_catalog_shards_are_strictly_below_nine_thousand_products() -> None:
    assert gjirafa50_catalog.MAX_GJIRAFA50_SHARD_PRODUCTS == 8_999
