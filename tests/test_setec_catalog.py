from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import FeedFetchError
from rss2discord.transports.setec_catalog import SetecCatalogClient
from tests.setec_helpers import (
    CATALOG_URL,
    RecordingGet,
    StubResponse,
    catalog_payload,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    product_payload,
)


def test_setec_catalog_client_fetches_full_catalog_in_api_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                catalog_payload(
                    3,
                    [
                        product_payload("prod-3", "product-3"),
                        product_payload("prod-2", "product-2"),
                    ],
                ),
            ),
            StubResponse(catalog_payload(3, [product_payload("prod-1", "product-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    products = SetecCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-3", "prod-2", "prod-1"]
    assert [parse_qs(urlsplit(url).query)["limit"][0] for url in get.urls] == [
        "250",
        "250",
    ]
    assert [parse_qs(urlsplit(url).query)["offset"][0] for url in get.urls] == [
        "0",
        "250",
    ]


def test_setec_catalog_client_collapses_identical_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    duplicate = product_payload("prod-1", "product-1")
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [duplicate])),
            StubResponse(catalog_payload(2, [duplicate])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    products = SetecCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-1"]


def test_setec_catalog_client_rejects_conflicting_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [product_payload("prod-1", "product-1")])),
            StubResponse(
                catalog_payload(
                    2,
                    [product_payload("prod-1", "changed-product-1", price=9_999)],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="DuplicateProductId"):
        SetecCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_setec_catalog_client_stops_before_a_later_page_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [product_payload("prod-2", "product-2")])),
            StubResponse(catalog_payload(2, [product_payload("prod-1", "product-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        SetecCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: len(get.urls) == 1,
        )

    # Then
    assert len(get.urls) == 1


def test_setec_catalog_client_restarts_the_complete_scan_after_a_retryable_later_page_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [product_payload("prod-2", "product-2")])),
            StubResponse(b"retry me", status_code=503),
            StubResponse(catalog_payload(2, [product_payload("prod-2", "product-2")])),
            StubResponse(catalog_payload(2, [product_payload("prod-1", "product-1")])),
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
    products = SetecCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=retry_policy,
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-2", "prod-1"]
    assert [parse_qs(urlsplit(url).query)["offset"][0] for url in get.urls] == [
        "0",
        "250",
        "0",
        "250",
    ]
    assert len(retry_delays) == 1


def test_setec_catalog_client_uses_the_latest_count_when_a_later_empty_page_completes_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                catalog_payload(
                    3,
                    [
                        product_payload("prod-3", "product-3"),
                        product_payload("prod-2", "product-2"),
                    ],
                ),
            ),
            StubResponse(catalog_payload(2, [])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    products = SetecCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-3", "prod-2"]
    assert [parse_qs(urlsplit(url).query)["offset"][0] for url in get.urls] == [
        "0",
        "250",
    ]


def test_setec_catalog_client_restarts_after_an_empty_page_before_declared_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(catalog_payload(2, [product_payload("prod-2", "product-2")])),
            StubResponse(catalog_payload(2, [])),
            StubResponse(catalog_payload(2, [product_payload("prod-2", "product-2")])),
            StubResponse(catalog_payload(2, [product_payload("prod-1", "product-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    retry_errors: list[FeedFetchError] = []

    def record_retry_sleep(seconds: float) -> bool:
        del seconds
        return True

    retry_policy = FetchRetryPolicy(
        sleep=record_retry_sleep,
        on_retry=lambda error, delay: retry_errors.append(error),
    )

    # When
    products = SetecCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=retry_policy,
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == ["prod-2", "prod-1"]
    assert [error.cause_type for error in retry_errors] == ["IncompleteCatalog"]
    assert all(error.retryable for error in retry_errors)
    assert [parse_qs(urlsplit(url).query)["offset"][0] for url in get.urls] == [
        "0",
        "250",
        "0",
        "250",
    ]
