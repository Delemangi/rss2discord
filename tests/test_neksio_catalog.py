from datetime import UTC
from decimal import Decimal

import pytest
import requests

from rss2discord.transports import FeedFetchError
from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from tests.neksio_helpers import (
    CATALOG_URL,
    RaisingPost,
    RecordingGet,
    RecordingPost,
    StubResponse,
    catalog_request,
    homepage_payload,
    page_payload,
    product_card,
)

type PaginationCase = tuple[int, int, int, int, str]


def test_fetch_catalog_extracts_categories_and_preserves_request_and_api_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = [product_card(product_id) for product_id in range(100, 200)]
    homepage = b'<div class="side-menu-category" data-bs-target="#cat_99"></div>'
    get = RecordingGet([StubResponse(homepage + homepage_payload([5, 5, 3]))])
    post = RecordingPost(
        [
            StubResponse(page_payload(5, 1, 2, 101, first_page)),
            StubResponse(page_payload(5, 2, 2, 101, [product_card(1)])),
            StubResponse(page_payload(3, 1, 1, 1, [product_card(9, old_price="1.500 ден.")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert [product.id for product in products] == [*range(100, 200), 1, 9]
    assert post.bodies == [catalog_request(5, 1), catalog_request(5, 2), catalog_request(3, 1)]
    assert products[0].amount == Decimal(1200)
    assert products[-1].old_formatted_price == "1.500 ден."
    assert {product.observed_at for product in products} == {products[0].observed_at}
    assert products[0].observed_at.tzinfo is UTC


def test_fetch_catalog_collapses_identical_products_from_different_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(homepage_payload([1, 2]))])
    post = RecordingPost(
        [
            StubResponse(page_payload(1, 1, 1, 1, [product_card(7)])),
            StubResponse(page_payload(2, 1, 1, 1, [product_card(7)])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert [product.id for product in products] == [7]


def test_fetch_catalog_rejects_conflicting_duplicate_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(homepage_payload([1, 2]))])
    post = RecordingPost(
        [
            StubResponse(page_payload(1, 1, 1, 1, [product_card(7)])),
            StubResponse(page_payload(2, 1, 1, 1, [product_card(7, name="Changed")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="DuplicateProductId"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_malformed_category_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                b'<div class="side-menu-category" data-bs-target="#subcat_wrong"></div>',
            ),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="MalformedCategoryMarkup"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_empty_category_enumeration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b'<div class="side-menu-category"></div>')])
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="EmptyCategoryEnumeration"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        page_payload(1, 1, 1, 1, [product_card(0)]),
    ],
)
def test_fetch_catalog_rejects_malformed_json_and_invalid_product_models(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(requests, "post", RecordingPost([StubResponse(payload)]))

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_oversized_homepage_and_streamed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    oversized_homepage = RecordingGet(
        [StubResponse(b"", headers={"Content-Length": "1048577"})],
    )
    monkeypatch.setattr(requests, "get", oversized_homepage)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(b"", chunks=(b"x" * 1_048_576, b"y"))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


@pytest.mark.parametrize(
    "case",
    [
        (2, 100, 1, 1, "PageNumberMismatch"),
        (1, 99, 1, 1, "PageSizeMismatch"),
        (1, 100, 1, 101, "PaginationMetadataMismatch"),
    ],
)
def test_fetch_catalog_rejects_inconsistent_pagination(
    monkeypatch: pytest.MonkeyPatch,
    case: PaginationCase,
) -> None:
    # Given
    page, page_size, no_of_pages, no_of_products, cause_type = case
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(
                    page_payload(
                        1,
                        page,
                        no_of_pages,
                        no_of_products,
                        [product_card(1)],
                        page_size=page_size,
                    ),
                ),
            ],
        ),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match=cause_type):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_follows_same_origin_redirects_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(b"", status_code=302, headers={"Location": "/"}),
            StubResponse(homepage_payload([1])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product_card(1)]))]),
    )

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert [product.id for product in products] == [1]
    assert get.urls == ["https://catalog.example.test/", "https://catalog.example.test/"]


def test_fetch_catalog_classifies_retryable_http_response_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(
                    b"failure",
                    status_code=429,
                    headers={"Retry-After": "2.5"},
                ),
            ],
        ),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert fetch_error.value.status_code == 429
    assert fetch_error.value.retryable
    assert fetch_error.value.retry_after == 2.5


@pytest.mark.parametrize("error", [requests.ConnectionError(), requests.Timeout()])
def test_fetch_catalog_marks_request_transport_interruptions_retryable(
    monkeypatch: pytest.MonkeyPatch,
    error: requests.RequestException,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(requests, "post", RaisingPost(error))

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert fetch_error.value.retryable
    assert fetch_error.value.cause_type == type(error).__name__


def test_fetch_catalog_marks_stream_interruptions_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(
                    b"",
                    interruption=requests.exceptions.ChunkedEncodingError(),
                ),
            ],
        ),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert fetch_error.value.retryable
    assert fetch_error.value.cause_type == "ChunkedEncodingError"


def test_fetch_catalog_rejects_urls_with_credentials_without_leaking_them() -> None:
    # Given
    credential = "top-secret"

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(
            f"https://user:{credential}@catalog.example.test/",
        )

    # Then
    assert fetch_error.value.cause_type == "InvalidUrl"
    assert credential not in str(fetch_error.value)
