import pytest
import requests

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import FeedFetchError, neptun_catalog
from rss2discord.transports.neptun_catalog import NeptunCatalogClient
from tests.neptun_helpers import (
    CATEGORY_URL,
    RecordingRequests,
    StubResponse,
    TruncatedResponse,
    category_html,
    never_shutdown,
    no_wait_retry_policy,
    product_payload,
    products_payload,
    requested_page,
)


def test_latest_products_requests_newest_thirty_and_returns_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests_spy = RecordingRequests(
        [StubResponse(category_html()), StubResponse(products_payload(2, [product_payload(2), product_payload(1)]))],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    products = NeptunCatalogClient().fetch_latest_products(CATEGORY_URL)

    assert [product.id for product in products] == [1, 2]
    assert requests_spy.calls[1][2] == {
        "model": {
            "TotalItems": 0,
            "CurrentPage": 1,
            "ItemsPerPage": 30,
            "Sort": 7,
            "CategoryId": 2,
            "Recomended": False,
            "ShowAllProducts": True,
        },
    }


def test_latest_products_rejects_page_over_requested_cardinality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    products = [product_payload(index) for index in range(1, 32)]
    requests_spy = RecordingRequests(
        [StubResponse(category_html()), StubResponse(products_payload(31, products))],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchError, match="PageCardinalityExceeded"):
        NeptunCatalogClient().fetch_latest_products(CATEGORY_URL)


def test_latest_products_rejects_incomplete_declared_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests_spy = RecordingRequests(
        [
            StubResponse(category_html()),
            StubResponse(products_payload(30, [product_payload(1)])),
        ],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchError, match="InvalidCardinality"):
        NeptunCatalogClient().fetch_latest_products(CATEGORY_URL)


def test_latest_products_rejects_duplicate_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = product_payload(1)
    requests_spy = RecordingRequests(
        [
            StubResponse(category_html()),
            StubResponse(products_payload(2, [duplicate, duplicate])),
        ],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchError, match="DuplicateProductId") as error:
        NeptunCatalogClient().fetch_latest_products(CATEGORY_URL)

    assert error.value.retryable


def test_complete_category_scan_rejects_identical_duplicate_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = product_payload(1)
    attempt = [
        StubResponse(category_html()),
        StubResponse(products_payload(2, [duplicate, duplicate])),
    ]
    requests_spy = RecordingRequests(attempt * 3)
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchError, match="DuplicateProductId") as error:
        NeptunCatalogClient().fetch_catalog(
            CATEGORY_URL,
            retry_policy=no_wait_retry_policy(),
            is_shutdown_requested=never_shutdown,
        )

    assert error.value.retryable


def test_complete_category_scan_rejects_conflicting_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = [
        StubResponse(category_html()),
        StubResponse(
            products_payload(
                2,
                [product_payload(1), product_payload(1, title="Changed")],
            ),
        ),
    ]
    requests_spy = RecordingRequests(attempt * 3)
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchError, match="DuplicateProductId") as error:
        NeptunCatalogClient().fetch_catalog(
            CATEGORY_URL,
            retry_policy=no_wait_retry_policy(),
            is_shutdown_requested=never_shutdown,
        )

    assert error.value.retryable


@pytest.mark.parametrize(
    ("total", "cause"),
    [(5_001, "ProductLimitExceeded"), (5_000, "IncompleteCatalog")],
)
def test_complete_category_scan_fails_closed_on_product_or_completion_bounds(
    monkeypatch: pytest.MonkeyPatch,
    total: int,
    cause: str,
) -> None:
    attempt = [StubResponse(category_html()), StubResponse(products_payload(total, []))]
    requests_spy = RecordingRequests(attempt if total > 5_000 else attempt * 3)
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchError, match=cause):
        NeptunCatalogClient().fetch_catalog(
            CATEGORY_URL,
            retry_policy=no_wait_retry_policy(),
            is_shutdown_requested=never_shutdown,
        )


def test_complete_category_scan_restarts_at_page_one_after_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = [product_payload(index) for index in range(1, 51)]
    requests_spy = RecordingRequests(
        [
            StubResponse(category_html()),
            StubResponse(products_payload(51, first_page)),
            StubResponse(b"retry", status_code=503),
            StubResponse(category_html()),
            StubResponse(products_payload(51, first_page)),
            StubResponse(products_payload(51, [product_payload(51)])),
        ],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)
    errors: list[str] = []

    products = NeptunCatalogClient().fetch_catalog(
        CATEGORY_URL,
        retry_policy=no_wait_retry_policy(errors),
        is_shutdown_requested=never_shutdown,
    )

    assert len(products) == 51
    assert errors == ["HTTPError"]
    assert [
        requested_page(call[2]) for call in requests_spy.calls if call[0] == "POST"
    ] == [1, 2, 1, 2]


def test_complete_category_scan_restarts_after_later_page_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = [product_payload(index) for index in range(1, 51)]
    requests_spy = RecordingRequests(
        [
            StubResponse(category_html()),
            StubResponse(products_payload(51, first_page)),
            TruncatedResponse(b'{"Batch":'),
            StubResponse(category_html()),
            StubResponse(products_payload(51, first_page)),
            StubResponse(products_payload(51, [product_payload(51)])),
        ],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)
    errors: list[str] = []

    products = NeptunCatalogClient().fetch_catalog(
        CATEGORY_URL,
        retry_policy=no_wait_retry_policy(errors),
        is_shutdown_requested=never_shutdown,
    )

    assert len(products) == 51
    assert errors == ["ChunkedEncodingError"]
    assert [
        requested_page(call[2]) for call in requests_spy.calls if call[0] == "POST"
    ] == [1, 2, 1, 2]


def test_complete_category_scan_checks_shutdown_between_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = [product_payload(index) for index in range(1, 51)]
    requests_spy = RecordingRequests(
        [StubResponse(category_html()), StubResponse(products_payload(51, first_page))],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchInterruptedError):
        NeptunCatalogClient().fetch_catalog(
            CATEGORY_URL,
            retry_policy=no_wait_retry_policy(),
            is_shutdown_requested=lambda: len([call for call in requests_spy.calls if call[0] == "POST"]) == 1,
        )

    assert len([call for call in requests_spy.calls if call[0] == "POST"]) == 1


def test_complete_category_scan_counts_bytes_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_products = [product_payload(index) for index in range(1, 51)]
    first_page = products_payload(51, first_products)
    second_page = products_payload(51, [product_payload(51)])
    monkeypatch.setattr(
        neptun_catalog,
        "MAX_NEPTUN_CATALOG_SCAN_BYTES",
        len(category_html()) + len(first_page) + len(second_page) - 1,
    )
    requests_spy = RecordingRequests(
        [StubResponse(category_html()), StubResponse(first_page), StubResponse(second_page)],
    )
    monkeypatch.setattr(requests, "get", requests_spy.get)
    monkeypatch.setattr(requests, "post", requests_spy.post)

    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        NeptunCatalogClient().fetch_catalog(
            CATEGORY_URL,
            retry_policy=no_wait_retry_policy(),
            is_shutdown_requested=never_shutdown,
        )
