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
    _OperationBudget,
)
from rss2discord.providers.gjirafa50.http import Gjirafa50HttpClient
from rss2discord.providers.gjirafa50.models import Gjirafa50PriceRange
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


def test_latest_products_logs_duplicate_id_with_discovery_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    duplicate_id = 8123
    get = RecordingGet(
        [StubResponse(catalog_payload(2, [(duplicate_id, 1), (duplicate_id, 1)]))],
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    client = Gjirafa50CatalogClient()
    with pytest.raises(FeedFetchError) as error:
        client.fetch_latest_products(ROOT_URL)

    assert error.value.cause_type == "DuplicateProductId"
    assert error.value.retryable is True
    assert len(get.params) == 1
    assert "phase=discovery page=1 product_id=8123 price_range=None" in caplog.text
    assert "Product 8123" not in caplog.text
    assert "/product-8123" not in caplog.text


def test_catalog_logs_duplicate_id_with_shard_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    duplicate_id = 8123
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [(duplicate_id, Decimal("0.01"))])),
            StubResponse(catalog_payload(2, [(duplicate_id, Decimal("0.01"))])),
            StubResponse(
                catalog_payload(
                    2,
                    [
                        (duplicate_id, Decimal("0.01")),
                        (duplicate_id, Decimal("0.02")),
                    ],
                ),
            ),
        ]
        * 3,
    )
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    client = Gjirafa50CatalogClient()
    fetch_retry_policy = no_wait_retry_policy()
    with pytest.raises(FeedFetchError) as error:
        client.fetch_catalog(
            ROOT_URL,
            retry_policy=fetch_retry_policy,
            is_shutdown_requested=lambda: False,
        )

    assert error.value.cause_type == "DuplicateProductId"
    assert error.value.retryable is True
    assert len(get.params) == 9
    assert "phase=enumeration page=1 product_id=8123 price_range=" in caplog.text
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


def test_catalog_fails_closed_when_individual_shard_count_changes_after_scan(
    monkeypatch: pytest.MonkeyPatch,
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
    get = RecordingGet([StubResponse(payload) for payload in responses * 3])
    monkeypatch.setattr(gjirafa50_http, "_create_session", lambda: get)

    client = Gjirafa50CatalogClient()
    fetch_retry_policy = no_wait_retry_policy()
    with pytest.raises(FeedFetchError, match="IncompleteCatalog"):
        client.fetch_catalog(
            ROOT_URL,
            retry_policy=fetch_retry_policy,
            is_shutdown_requested=lambda: False,
        )


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
        ],
    )

    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with Gjirafa50HttpClient(get) as http, pytest.raises(FeedFetchError) as error:
        client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert error.value.cause_type == "CatalogChanged"
    assert error.value.retryable is True
    assert "phase=root_recount page=1 expected=1 actual=2" in caplog.text
    assert "attempt=1 outcome=failed cause=CatalogChanged root_total=1" in caplog.text
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
        ],
    )

    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with Gjirafa50HttpClient(get) as http, pytest.raises(FeedFetchError) as error:
        client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert error.value.cause_type == "CatalogChanged"
    assert error.value.retryable is True
    assert (
        "phase=enumeration page=1 price_range=0-2147483647 "
        "expected_hits=1 actual_hits=2 expected_pages=1 actual_pages=2"
    ) in caplog.text
    assert "https://" not in caplog.text


def test_excess_unique_ids_log_numeric_catalog_diagnostic(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 1)
    monkeypatch.setattr(
        gjirafa50_catalog,
        "MAX_GJIRAFA50_PRICE_EXCLUSIVE_CENTS",
        4,
    )
    responses = [
        catalog_payload(2, [(1, Decimal(0))]),
        catalog_payload(2, [(1, Decimal(0))]),
        catalog_payload(1, [(1, Decimal(0))]),
        catalog_payload(2, [(2, Decimal("0.01")), (3, Decimal("0.02"))]),
        catalog_payload(1, [(2, Decimal("0.01"))]),
        catalog_payload(1, [(3, Decimal("0.02"))]),
        catalog_payload(1, [(1, Decimal(0))]),
        catalog_payload(1, [], total_pages=1),
        catalog_payload(1, [(2, Decimal("0.01"))]),
        catalog_payload(1, [], total_pages=1),
        catalog_payload(1, [(3, Decimal("0.02"))]),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])

    client = Gjirafa50CatalogClient()
    operation_budget = _OperationBudget(lambda: False)
    with Gjirafa50HttpClient(get) as http, pytest.raises(FeedFetchError) as error:
        client._scan_catalog(
            ROOT_URL,
            operation_budget,
            http,
        )

    assert error.value.cause_type == "CatalogChanged"
    assert error.value.retryable is True
    assert (
        "phase=enumeration page=1 price_range=0,02-0,03 root_total=2 unique_ids=3"
    ) in caplog.text
    assert "https://" not in caplog.text


def test_retry_attempt_summary_reports_fresh_inventory_and_delta_counters(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
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
        "attempt=1 outcome=failed cause=CatalogChanged root_total=1 unique_ids=1"
        in summaries[0]
    )
    assert "request_budget_checks_delta=5 request_budget_checks_total=5" in summaries[0]
    assert "parsed_cards_delta=4 parsed_cards_total=4" in summaries[0]
    assert (
        "attempt=2 outcome=success cause=none root_total=1 unique_ids=1" in summaries[1]
    )
    assert (
        "request_budget_checks_delta=6 request_budget_checks_total=11" in summaries[1]
    )
    assert "parsed_cards_delta=5 parsed_cards_total=9" in summaries[1]
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
