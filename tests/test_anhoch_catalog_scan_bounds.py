import pytest
import requests

from rss2discord.transports import FeedFetchError, anhoch_catalog
from rss2discord.transports.anhoch_catalog import AnhochCatalogClient
from tests.anhoch_helpers import (
    CATALOG_URL,
    RecordingGet,
    StubResponse,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    page_payload,
    product_payload,
)


def test_anhoch_latest_scan_counts_redirect_body_bytes_and_stops_before_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = page_payload(1, 3, [product_payload(1, "p-1")])
    second_page = page_payload(2, 3, [product_payload(2, "p-2")])
    padded_first_page = first_page + b" " * (
        anhoch_catalog.MAX_ANHOCH_LATEST_RESPONSE_BYTES - len(first_page)
    )
    padded_second_page = second_page + b" " * (
        anhoch_catalog.MAX_ANHOCH_LATEST_RESPONSE_BYTES - len(second_page)
    )
    get = RecordingGet(
        [
            StubResponse(padded_first_page),
            StubResponse(
                b"x" * anhoch_catalog.MAX_ANHOCH_LATEST_RESPONSE_BYTES,
                status_code=302,
                headers={"Location": "/products?page=2"},
            ),
            StubResponse(padded_second_page),
            StubResponse(
                b"x",
                status_code=302,
                headers={"Location": "/must-not-follow"},
            ),
            StubResponse(page_payload(3, 3, [product_payload(3, "p-3")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        AnhochCatalogClient().fetch_latest_products(CATALOG_URL)
    assert len(get.urls) == 3
    assert len(get.responses) == 2


def test_anhoch_latest_scan_does_not_follow_a_redirect_after_its_body_exhausts_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = page_payload(1, 2, [product_payload(1, "p-1")])
    padded_first_page = first_page + b" " * (
        anhoch_catalog.MAX_ANHOCH_LATEST_RESPONSE_BYTES - len(first_page)
    )
    get = RecordingGet(
        [
            StubResponse(
                b"x" * anhoch_catalog.MAX_ANHOCH_LATEST_RESPONSE_BYTES,
                status_code=302,
                headers={"Location": "/products?page=1"},
            ),
            StubResponse(padded_first_page),
            StubResponse(
                b"x" * anhoch_catalog.MAX_ANHOCH_LATEST_RESPONSE_BYTES,
                status_code=302,
                headers={"Location": "/must-not-follow"},
            ),
            StubResponse(page_payload(1, 1, [product_payload(1, "p-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        AnhochCatalogClient().fetch_latest_products(CATALOG_URL)
    assert len(get.urls) == 3
    assert len(get.responses) == 1


def test_anhoch_catalog_client_resets_scan_bytes_for_a_complete_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = page_payload(1, 2, [product_payload(2, "p-2")])
    second_page = page_payload(2, 2, [product_payload(1, "p-1")])
    monkeypatch.setattr(
        anhoch_catalog,
        "MAX_ANHOCH_CATALOG_SCAN_BYTES",
        len(first_page) + len(second_page),
    )
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet(
            [
                StubResponse(first_page),
                StubResponse(b"retry", status_code=503),
                StubResponse(first_page),
                StubResponse(second_page),
            ],
        ),
    )

    # When
    products = AnhochCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.id for product in products] == [2, 1]


def test_anhoch_catalog_client_accepts_a_valid_twenty_page_ten_thousand_product_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    page_count = 20
    products_per_page = anhoch_catalog.ANHOCH_CATALOG_PRODUCTS_PER_PAGE
    responses = [
        StubResponse(
            page_payload(
                page_number,
                page_count,
                [
                    product_payload(
                        (page_number - 1) * products_per_page + product_number,
                        f"p-{(page_number - 1) * products_per_page + product_number}",
                    )
                    for product_number in range(1, products_per_page + 1)
                ],
            ),
        )
        for page_number in range(1, page_count + 1)
    ]
    get = RecordingGet(responses)
    monkeypatch.setattr(requests, "get", get)

    # When
    products = AnhochCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert len(products) == 10_000
    assert (products[0].id, products[-1].id) == (1, 10_000)
    assert len(get.urls) == page_count
