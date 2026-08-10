import pytest
import requests

from rss2discord.transports import FeedFetchError, setec_catalog, setec_catalog_bounds
from rss2discord.transports.setec_catalog import SetecCatalogClient
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_CATALOG_PRODUCTS,
    MAX_SETEC_CATALOG_RESPONSE_BYTES,
    SETEC_SEARCH_PAGE_SIZE,
    SETEC_WINDOW_SIZE,
)
from tests.setec_helpers import (
    CATALOG_URL,
    FakeMeilisearchIndex,
    IndexedProduct,
    RecordingPost,
    StubResponse,
    catalog_scan_should_stop,
    count_payload,
    no_wait_fetch_retry_policy,
    price_payload,
    product_payload,
    search_payload,
)

SEARCH_REQUEST_BUDGET = 6
BAND_CORPUS_SIZE = 2_048


def bisectable_corpus(size: int) -> tuple[IndexedProduct, ...]:
    """Build a corpus whose distinct amounts force repeated band bisection."""
    return tuple(
        IndexedProduct(f"prod-{amount:05d}", amount) for amount in range(1, size + 1)
    )


def test_setec_catalog_client_rejects_a_declared_catalog_larger_than_the_product_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                count_payload([1_499] * (MAX_SETEC_CATALOG_PRODUCTS + 1)),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert len(post.urls) == 1


def test_setec_catalog_client_rejects_a_band_response_larger_than_the_response_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(count_payload([1_499])),
            StubResponse(count_payload([1_499])),
            StubResponse(
                search_payload([price_payload("prod-1")]),
                headers={
                    "Content-Length": str(MAX_SETEC_CATALOG_RESPONSE_BYTES + 1),
                },
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert len(post.urls) == 3


def test_setec_catalog_client_rejects_total_response_bytes_larger_than_the_scan_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    summary_count = count_payload([1_499, 2_499])
    band_count = count_payload([1_499, 2_499])
    post = RecordingPost([StubResponse(summary_count), StubResponse(band_count)])
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(
        setec_catalog_bounds,
        "MAX_SETEC_CATALOG_SCAN_BYTES",
        len(summary_count) + len(band_count) - 1,
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert len(post.urls) == 2


def test_setec_catalog_client_rejects_a_scan_that_exhausts_the_search_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    index = FakeMeilisearchIndex(bisectable_corpus(BAND_CORPUS_SIZE))
    monkeypatch.setattr(requests, "post", index)
    monkeypatch.setattr(
        setec_catalog,
        "MAX_SETEC_SEARCH_REQUESTS",
        SEARCH_REQUEST_BUDGET,
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="SearchRequestLimitExceeded"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert len(index.bodies) == SEARCH_REQUEST_BUDGET
    assert index.count_request_total > 1


def test_setec_catalog_client_rejects_a_band_page_larger_than_the_search_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    band_count = count_payload([1_499] * SETEC_SEARCH_PAGE_SIZE)
    post = RecordingPost(
        [
            StubResponse(band_count),
            StubResponse(band_count),
            StubResponse(
                search_payload(
                    [
                        price_payload(f"prod-{index:05d}")
                        for index in range(SETEC_SEARCH_PAGE_SIZE + 1)
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="PageCardinalityExceeded"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )

    # Then
    assert len(post.urls) == 3


def test_setec_catalog_client_rejects_a_discovery_window_larger_than_the_window_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                search_payload(
                    [
                        product_payload(f"prod-{index:05d}", f"product-{index:05d}")
                        for index in range(SETEC_WINDOW_SIZE + 1)
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="PageCardinalityExceeded"):
        SetecCatalogClient().fetch_latest_products(CATALOG_URL)

    # Then
    assert len(post.urls) == 1
