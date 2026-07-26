import pytest
import requests

from rss2discord.transports import FeedFetchError, setec_catalog_bounds
from rss2discord.transports.setec_catalog import SetecCatalogClient
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_CATALOG_PAGES,
    MAX_SETEC_CATALOG_PRODUCTS,
    SETEC_CATALOG_PAGE_SIZE,
)
from tests.setec_helpers import (
    CATALOG_URL,
    RecordingGet,
    StubResponse,
    catalog_payload,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    product_payload,
)


def test_setec_catalog_client_rejects_a_page_larger_than_the_catalog_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                catalog_payload(
                    SETEC_CATALOG_PAGE_SIZE + 1,
                    [
                        product_payload(f"prod-{index}", f"product-{index}")
                        for index in range(SETEC_CATALOG_PAGE_SIZE + 1)
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="PageCardinalityExceeded"):
        SetecCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_setec_catalog_client_rejects_a_declared_catalog_larger_than_the_product_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [StubResponse(catalog_payload(MAX_SETEC_CATALOG_PRODUCTS + 1, []))],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        SetecCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_setec_catalog_client_rejects_a_scan_that_reaches_the_page_bound_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                catalog_payload(
                    MAX_SETEC_CATALOG_PRODUCTS,
                    [product_payload(f"prod-{index}", f"product-{index}")],
                ),
            )
            for index in range(MAX_SETEC_CATALOG_PAGES)
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        SetecCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_setec_catalog_client_rejects_total_response_bytes_larger_than_the_scan_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = catalog_payload(2, [product_payload("prod-2", "product-2")])
    second_page = catalog_payload(2, [product_payload("prod-1", "product-1")])
    get = RecordingGet([StubResponse(first_page), StubResponse(second_page)])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(
        setec_catalog_bounds,
        "MAX_SETEC_CATALOG_SCAN_BYTES",
        len(first_page) + len(second_page) - 1,
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        SetecCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
