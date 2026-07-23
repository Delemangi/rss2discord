import pytest
import requests

from rss2discord.transports import FeedFetchError, anhoch_catalog
from rss2discord.transports.anhoch_catalog import AnhochCatalogClient
from rss2discord.transports.anhoch_catalog_bounds import (
    CatalogScanBounds,
    CatalogScanTotals,
    FetchedCatalogPage,
)
from rss2discord.transports.anhoch_models import AnhochCatalogResponse, AnhochProduct
from tests.anhoch_helpers import (
    CATALOG_URL,
    RecordingGet,
    StubResponse,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    page_payload,
    product_payload,
)


def test_anhoch_catalog_client_rejects_response_too_large_by_declared_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b"{}", headers={"Content-Length": "2097153"})])
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_anhoch_catalog_client_rejects_response_too_large_by_streamed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                b"{}",
                chunks=(b"x" * 1_048_576, b"y" * 1_048_576, b"z"),
            ),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_anhoch_catalog_client_rejects_page_100_ceiling_when_more_pages_are_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    responses = [
        StubResponse(page_payload(page, 101, [product_payload(page, f"p-{page}")]))
        for page in range(1, 101)
    ]
    monkeypatch.setattr(requests, "get", RecordingGet(responses))

    # When / Then
    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_anhoch_catalog_client_rejects_a_page_with_more_products_than_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    products = [
        product_payload(product_id, f"p-{product_id}") for product_id in range(1, 502)
    ]
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(page_payload(1, 1, products))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PageCardinalityExceeded"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_anhoch_catalog_client_rejects_product_id_above_sqlite_signed_integer_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_payload(2**63, "outside-sqlite-range")
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(page_payload(1, 1, [product]))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_anhoch_catalog_client_rejects_a_near_limit_oversized_page_before_retaining_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    products = [
        product_payload(product_id, f"p-{product_id}") for product_id in range(1, 502)
    ]
    for product in products:
        product["name"] = "x" * 3600
    payload = page_payload(1, 1, products)
    assert 1_900_000 < len(payload) < anhoch_catalog.MAX_ANHOCH_CATALOG_RESPONSE_BYTES
    retained = False

    def fail_if_retained(
        target: list[AnhochProduct],
        seen: dict[int, AnhochProduct],
        page_products: tuple[AnhochProduct, ...],
    ) -> None:
        nonlocal retained
        del target, seen, page_products
        retained = True
        raise AssertionError("oversized page must not be retained")

    monkeypatch.setattr(
        AnhochCatalogClient,
        "_append_unique_products",
        staticmethod(fail_if_retained),
    )
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(payload)]))

    # When / Then
    with pytest.raises(FeedFetchError, match="PageCardinalityExceeded"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert not retained


def test_anhoch_catalog_client_rejects_a_page_number_that_does_not_match_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(page_payload(2, 2, [product_payload(1, "p-1")]))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PageNumberMismatch"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_anhoch_catalog_client_rejects_an_impossible_last_page_on_the_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [StubResponse(page_payload(1, 101, [product_payload(1, "p-1")]))],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        AnhochCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert len(get.urls) == 1


def test_anhoch_catalog_scan_bounds_reject_product_count_past_capacity() -> None:
    # Given
    page = AnhochCatalogResponse.model_validate_json(
        page_payload(1, 100, [product_payload(1, "p-1")]),
    ).products
    bounds = CatalogScanBounds(
        per_page=anhoch_catalog.ANHOCH_CATALOG_PRODUCTS_PER_PAGE,
        max_pages=anhoch_catalog.MAX_ANHOCH_CATALOG_PAGES,
        max_scan_bytes=anhoch_catalog.MAX_ANHOCH_CATALOG_SCAN_BYTES,
        require_complete_catalog=True,
    )
    totals = CatalogScanTotals(
        product_count=(
            anhoch_catalog.ANHOCH_CATALOG_PRODUCTS_PER_PAGE
            * anhoch_catalog.MAX_ANHOCH_CATALOG_PAGES
        ),
        response_bytes=0,
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        bounds.validate_page(
            FetchedCatalogPage(page=page, response_bytes=0),
            requested_page_number=1,
            totals=totals,
        )


@pytest.mark.parametrize(
    "max_scan_bytes",
    [
        anhoch_catalog.MAX_ANHOCH_LATEST_SCAN_BYTES,
        anhoch_catalog.MAX_ANHOCH_CATALOG_SCAN_BYTES,
    ],
)
def test_anhoch_catalog_scan_bounds_reject_cumulative_bytes_past_the_scan_limit(
    max_scan_bytes: int,
) -> None:
    # Given
    page = AnhochCatalogResponse.model_validate_json(
        page_payload(1, 1, [product_payload(1, "p-1")]),
    ).products
    bounds = CatalogScanBounds(
        per_page=1,
        max_pages=1,
        max_scan_bytes=max_scan_bytes,
        require_complete_catalog=True,
    )
    totals = CatalogScanTotals(
        product_count=0,
        response_bytes=max_scan_bytes - 1,
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        bounds.validate_page(
            FetchedCatalogPage(page=page, response_bytes=2),
            requested_page_number=1,
            totals=totals,
        )
