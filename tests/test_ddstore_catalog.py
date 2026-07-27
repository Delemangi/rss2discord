import pytest

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import ddstore_http
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.ddstore_catalog import DDStoreCatalogClient
from tests.ddstore_helpers import (
    CATALOG_URL,
    RecordingPost,
    StubResponse,
    catalog_payload,
    catalog_scan_should_stop,
    no_wait_fetch_retry_policy,
    product_payload,
)


def test_ddstore_catalog_client_fetches_complete_catalog_with_exact_unique_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = [
        product_payload(str(product_number)) for product_number in range(1, 501)
    ]
    post = RecordingPost(
        [
            StubResponse(catalog_payload(501, first_page, current_page=1)),
            StubResponse(
                catalog_payload(501, [product_payload("501")], current_page=2),
            ),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When
    products = DDStoreCatalogClient().fetch_catalog(
        CATALOG_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=catalog_scan_should_stop,
    )

    # Then
    assert [product.uid for product in products] == [
        *[str(product_number) for product_number in range(1, 501)],
        "501",
    ]
    current_pages = []
    for payload in post.payloads:
        variables = payload["variables"]
        assert isinstance(variables, dict)
        current_page = variables["currentPage"]
        assert isinstance(current_page, int)
        current_pages.append(current_page)
    assert current_pages == [1, 2]


def test_ddstore_catalog_client_rejects_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = [
        product_payload(str(product_number)) for product_number in range(1, 501)
    ]
    post = RecordingPost(
        [
            StubResponse(catalog_payload(501, first_page, current_page=1)),
            StubResponse(catalog_payload(502, [product_payload("2")], current_page=2)),
            StubResponse(catalog_payload(501, first_page, current_page=1)),
            StubResponse(catalog_payload(502, [product_payload("2")], current_page=2)),
            StubResponse(catalog_payload(501, first_page, current_page=1)),
            StubResponse(catalog_payload(502, [product_payload("2")], current_page=2)),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="CatalogMetadataDrift"):
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_ddstore_catalog_client_rejects_conflicting_duplicate_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    responses = [
        response
        for _ in range(3)
        for response in (
            StubResponse(
                catalog_payload(
                    501,
                    [product_payload(str(index)) for index in range(1, 501)],
                    current_page=1,
                ),
            ),
            StubResponse(
                catalog_payload(
                    501,
                    [product_payload("1", price=9_999, regular_price=9_999)],
                    current_page=2,
                ),
            ),
        )
    ]
    post = RecordingPost(responses)
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="PaginationDrift") as fetch_error:
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert fetch_error.value.retryable


def test_ddstore_catalog_client_rejects_identical_duplicate_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = [product_payload(str(index)) for index in range(1, 501)]
    responses = [
        response
        for _ in range(3)
        for response in (
            StubResponse(catalog_payload(501, first_page, current_page=1)),
            StubResponse(catalog_payload(501, [product_payload("1")], current_page=2)),
        )
    ]
    monkeypatch.setattr(
        ddstore_http,
        "_create_session",
        lambda: RecordingPost(responses),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PaginationDrift") as fetch_error:
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert fetch_error.value.retryable


@pytest.mark.parametrize("item_count", [1, 501])
def test_ddstore_catalog_client_rejects_nonfinal_page_cardinality_drift(
    monkeypatch: pytest.MonkeyPatch,
    item_count: int,
) -> None:
    # Given
    responses = [
        StubResponse(
            catalog_payload(
                501,
                [product_payload(str(index)) for index in range(item_count)],
                current_page=1,
            ),
        )
        for _ in range(6)
    ]
    monkeypatch.setattr(
        ddstore_http,
        "_create_session",
        lambda: RecordingPost(responses),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PaginationDrift") as fetch_error:
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert fetch_error.value.retryable


def test_ddstore_catalog_client_rejects_complete_empty_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    responses = [StubResponse(catalog_payload(0, [], current_page=1)) for _ in range(3)]
    monkeypatch.setattr(
        ddstore_http,
        "_create_session",
        lambda: RecordingPost(responses),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="EmptyCatalog") as fetch_error:
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert fetch_error.value.retryable


@pytest.mark.parametrize("final_item_count", [0, 2])
def test_ddstore_catalog_client_rejects_final_page_cardinality_drift(
    monkeypatch: pytest.MonkeyPatch,
    final_item_count: int,
) -> None:
    # Given
    first_page = [product_payload(str(index)) for index in range(1, 501)]
    responses = [
        response
        for _ in range(3)
        for response in (
            StubResponse(catalog_payload(501, first_page, current_page=1)),
            StubResponse(
                catalog_payload(
                    501,
                    [
                        product_payload(str(index))
                        for index in range(501, 501 + final_item_count)
                    ],
                    current_page=2,
                ),
            ),
        )
    ]
    monkeypatch.setattr(
        ddstore_http,
        "_create_session",
        lambda: RecordingPost(responses),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PaginationDrift") as fetch_error:
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )
    assert fetch_error.value.retryable


def test_ddstore_catalog_client_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [StubResponse(b"{}", headers={"content-length": str(2_097_153)})],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=catalog_scan_should_stop,
        )


def test_ddstore_catalog_client_honors_shutdown_before_next_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                catalog_payload(
                    501,
                    [product_payload(str(index)) for index in range(1, 501)],
                    current_page=1,
                ),
            ),
            StubResponse(catalog_payload(501, [product_payload("2")], current_page=2)),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        DDStoreCatalogClient().fetch_catalog(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: len(post.urls) == 1,
        )
    assert len(post.urls) == 1
