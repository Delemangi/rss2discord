from decimal import Decimal

import pytest
import requests

from rss2discord.transports import FeedFetchError, gjirafa50_catalog
from rss2discord.transports.gjirafa50_catalog import (
    Gjirafa50CatalogClient,
    _OperationBudget,
)
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
    monkeypatch.setattr(requests.Session, "get", get)

    # When
    products = Gjirafa50CatalogClient().fetch_latest_products(ROOT_URL)

    # Then
    assert [product.id for product in products] == list(range(1, 31))
    assert [params["pagenumber"] for params in get.params] == [1, 2]
    assert all(params["orderby"] == 16 for params in get.params)


def test_catalog_recursively_shards_prices_and_reconciles_all_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 2)
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_PRICE", 3)
    responses = [
        catalog_payload(4, [(4, 3)]),
        catalog_payload(4, [(4, 3)]),
        catalog_payload(2, [(1, 0), (2, 1)]),
        catalog_payload(2, [(3, 2), (4, 3)]),
        catalog_payload(2, [(1, 0), (2, 1)]),
        catalog_payload(2, [], total_pages=1),
        catalog_payload(2, [(3, 2), (4, 3)]),
        catalog_payload(2, [], total_pages=1),
        catalog_payload(4, [(4, 3)]),
        catalog_payload(2, [(1, 0), (2, 1)]),
        catalog_payload(2, [(3, 2), (4, 3)]),
    ]
    get = RecordingGet([StubResponse(payload) for payload in responses])
    monkeypatch.setattr(requests.Session, "get", get)

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
        Decimal(1),
        Decimal(2),
        Decimal(3),
    ]
    requested_ranges = [params.get("price") for params in get.params]
    assert requested_ranges.count("0-1") == 4
    assert requested_ranges.count("2-3") == 4


def test_catalog_fails_closed_when_shard_counts_do_not_cover_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 2)
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_PRICE", 3)
    get = RecordingGet(
        [
            StubResponse(catalog_payload(4, [(4, 3)])),
            StubResponse(catalog_payload(4, [(4, 3)])),
            StubResponse(catalog_payload(2, [(1, 0), (2, 1)])),
            StubResponse(catalog_payload(1, [(3, 2)])),
        ]
        * 3,
    )
    monkeypatch.setattr(requests.Session, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="CatalogChanged"):
        Gjirafa50CatalogClient().fetch_catalog(
            ROOT_URL,
            retry_policy=no_wait_retry_policy(),
            is_shutdown_requested=lambda: False,
        )


def test_catalog_request_budget_is_shared_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_PAGES", 3)
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_SHARD_PRODUCTS", 1)
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_PRICE", 1)
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [(2, 1)])),
            StubResponse(catalog_payload(2, [(2, 1)])),
            StubResponse(b"retry", status_code=503),
        ],
    )
    monkeypatch.setattr(requests.Session, "get", get)

    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        Gjirafa50CatalogClient().fetch_catalog(
            ROOT_URL,
            retry_policy=no_wait_retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert len(get.params) == 3


def test_catalog_product_budget_accumulates_across_retry_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gjirafa50_catalog, "MAX_GJIRAFA50_PRODUCTS", 3)
    budget = _OperationBudget(lambda: False)

    budget.consume_products(2)

    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
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
