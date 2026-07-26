import pytest
import requests

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import FeedFetchError, neksio_catalog
from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from tests.neksio_helpers import (
    CATALOG_URL,
    RecordingGet,
    RecordingPost,
    StubResponse,
    homepage_payload,
    page_payload,
    product_card,
)


def test_fetch_catalog_accepts_omitted_old_price_with_none_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    del product["old_PriceWTax"]
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert products[0].old_formatted_price is None


def test_fetch_catalog_accepts_null_manufacturer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    product["manufacturer"] = None
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert products[0].manufacturer == ""


def test_fetch_catalog_accepts_null_subcategory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    product["subCategory"] = None
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert products[0].subcategory == ""


def test_fetch_catalog_normalizes_negative_stock_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    product["quantity"] = -1
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert products[0].stock_quantity == 0


def test_fetch_catalog_rejects_stock_below_negative_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    product["quantity"] = -2
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_oversized_formatted_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    product["priceWTax_f"] = "x" * 129
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_stops_before_the_next_page_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page_products = [product_card(product_id) for product_id in range(1, 101)]
    get = RecordingGet([StubResponse(homepage_payload([1]))])
    post = RecordingPost(
        [
            StubResponse(page_payload(1, 1, 2, 101, first_page_products)),
            StubResponse(page_payload(1, 2, 2, 101, [product_card(101)])),
        ],
    )
    shutdown_checks = iter((False, True))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        NeksioCatalogClient().fetch_catalog(
            CATALOG_URL,
            is_shutdown_requested=lambda: next(shutdown_checks),
        )
    assert len(post.urls) == 1


def test_fetch_catalog_rejects_price_precision_the_snapshot_store_cannot_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    product["priceWTax"] = 1.12345
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_price_magnitude_the_snapshot_store_cannot_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_card(1)
    product["priceWTax"] = 1_000_000_000_000
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_oversized_category_ids_before_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    category_id = 9_999_999_999_999_999_999
    homepage = (
        f'<div class="side-menu-category" data-bs-target="#subcat_{category_id}"></div>'
    ).encode()
    get = RecordingGet([StubResponse(homepage)])
    post = RecordingPost([])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="MalformedCategoryMarkup"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    assert len(get.urls) == 1
    assert post.urls == []


def test_fetch_catalog_enforces_the_category_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(neksio_catalog, "MAX_NEKSIO_CATEGORIES", 1)
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1, 2]))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="CategoryLimitExceeded"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_enforces_the_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    page_limit = neksio_catalog.MAX_NEKSIO_PAGES_PER_CATEGORY + 1
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [StubResponse(page_payload(1, 1, page_limit, 1, [product_card(1)]))],
        ),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_enforces_the_unique_product_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(neksio_catalog, "MAX_NEKSIO_PRODUCTS", 1)
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(
                    page_payload(1, 1, 1, 2, [product_card(1), product_card(2)]),
                ),
            ],
        ),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_pagination_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_page = [product_card(product_id) for product_id in range(100, 200)]
    second_page = [product_card(product_id) for product_id in range(200, 300)]
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(page_payload(1, 1, 2, 101, first_page)),
                StubResponse(page_payload(1, 2, 3, 201, second_page)),
                StubResponse(page_payload(1, 3, 3, 201, [product_card(300)])),
            ],
        ),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="PaginationMetadataDrift"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)
