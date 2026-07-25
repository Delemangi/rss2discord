from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import FeedFetchError
from rss2discord.transports.anhoch_catalog import AnhochCatalogClient
from tests.anhoch_helpers import (
    CATALOG_URL,
    RecordingGet,
    StubResponse,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    page_payload,
    product_payload,
    requested_page_numbers,
)


def test_anhoch_catalog_client_accepts_formatted_only_ancillary_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_payload(1, "product-0001")
    product["price"] = {"formatted": "1.500,00 ден."}
    product["installments"] = {
        "period": 24,
        "price": {"formatted": "50,00 ден."},
    }
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(page_payload(1, 1, [product]))]),
    )

    # When
    products = AnhochCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert products[0].price.formatted == "1.500,00 ден."
    assert products[0].installments is not None
    assert products[0].installments.price.formatted == "50,00 ден."


def test_anhoch_catalog_client_fetches_full_catalog_in_api_order_and_stops_on_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                page_payload(
                    1,
                    99,
                    [product_payload(3, "p-3"), product_payload(2, "p-2")],
                ),
            ),
            StubResponse(page_payload(2, 99, [])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    products = AnhochCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == [3, 2]
    assert [
        query["perPage"][0]
        for query in (parse_qs(urlsplit(url).query) for url in get.urls)
    ] == ["500", "500"]
    assert requested_page_numbers(get.urls) == ["1", "2"]


def test_anhoch_catalog_client_stops_before_the_next_page_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(2, "p-2")])),
            StubResponse(page_payload(2, 2, [product_payload(1, "p-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: len(get.urls) == 1,
        )

    assert requested_page_numbers(get.urls) == ["1"]


def test_catalog_scan_discards_filters_while_latest_scan_preserves_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    filtered_url = (
        "https://www.anhoch.com/products?query=keyboard&inStockOnly=2&"
        "brand=example&sort=price_asc&perPage=25&page=9"
    )
    full_catalog_get = RecordingGet([StubResponse(page_payload(1, 1, []))])
    latest_products_get = RecordingGet([StubResponse(page_payload(1, 1, []))])
    client = AnhochCatalogClient()
    monkeypatch.setattr(requests, "get", full_catalog_get)

    # When
    _ = client.fetch_catalog(
        filtered_url,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )
    monkeypatch.setattr(requests, "get", latest_products_get)
    _ = client.fetch_latest_products(filtered_url)

    # Then
    assert parse_qs(urlsplit(full_catalog_get.urls[0]).query) == {
        "sort": ["latest"],
        "perPage": ["500"],
        "page": ["1"],
    }
    assert parse_qs(urlsplit(latest_products_get.urls[0]).query) == {
        "query": ["keyboard"],
        "inStockOnly": ["2"],
        "brand": ["example"],
        "sort": ["latest"],
        "perPage": ["30"],
        "page": ["1"],
    }


def test_anhoch_catalog_client_restarts_full_scan_after_retryable_later_page_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(2, "p-2")])),
            StubResponse(
                b"retry me",
                status_code=503,
                headers={"Retry-After": "9999"},
            ),
            StubResponse(page_payload(1, 2, [product_payload(2, "p-2")])),
            StubResponse(page_payload(2, 2, [product_payload(1, "p-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    retry_delays: list[float] = []

    def record_retry_sleep(seconds: float) -> bool:
        retry_delays.append(seconds)
        return True

    retry_policy = FetchRetryPolicy(
        sleep=record_retry_sleep,
        on_retry=lambda error, delay: None,
    )

    # When
    products = AnhochCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=retry_policy,
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == [2, 1]
    assert requested_page_numbers(get.urls) == ["1", "2", "1", "2"]
    assert retry_delays == [300.0]


def test_anhoch_catalog_client_continues_through_declared_final_page_and_preserves_api_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(3, "p-3")])),
            StubResponse(
                page_payload(
                    2,
                    2,
                    [product_payload(2, "p-2"), product_payload(1, "p-1")],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    products = AnhochCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == [3, 2, 1]


def test_anhoch_catalog_client_collapses_identical_duplicates_and_rejects_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    identical_get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(1, "p-1")])),
            StubResponse(page_payload(2, 2, [product_payload(1, "p-1")])),
        ],
    )
    conflicting_get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(1, "p-1")])),
            StubResponse(
                page_payload(
                    2,
                    2,
                    [
                        product_payload(
                            1,
                            "p-1-changed",
                            selling_price="9.999,00 ден.",
                            selling_price_amount="9999.00",
                        ),
                    ],
                ),
            ),
        ],
    )

    # When
    monkeypatch.setattr(requests, "get", identical_get)
    products = AnhochCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == [1]

    # When / Then
    monkeypatch.setattr(requests, "get", conflicting_get)
    with pytest.raises(FeedFetchError, match="DuplicateProductId"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
