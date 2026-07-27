import pytest
from pydantic import ValidationError

from rss2discord.models import SourceMetric
from rss2discord.transports import ddstore_http
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.ddstore import DDStoreStrategy
from rss2discord.transports.ddstore_http import DDSTORE_USER_AGENT
from rss2discord.transports.ddstore_models import DDStoreProduct
from tests.ddstore_helpers import (
    CATALOG_URL,
    RecordingPost,
    StubResponse,
    catalog_payload,
    product_payload,
)


def test_ddstore_strategy_posts_required_empty_search_and_sorts_latest_products_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                catalog_payload(
                    3,
                    [
                        product_payload("3", created_at="2024-07-10 08:54:25"),
                        product_payload("2", created_at="2024-07-09 08:54:25"),
                        product_payload("1", created_at="2024-07-09 08:54:25"),
                    ],
                    current_page=1,
                ),
            ),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)
    strategy = DDStoreStrategy()

    # When
    entries, source_title = strategy.fetch_entries(CATALOG_URL)
    data = strategy.get_entry_data(entries[0])

    # Then
    assert source_title == "DDStore"
    assert strategy.seed_existing_on_first_fetch
    assert [strategy.get_entry_id(entry) for entry in entries] == ["1", "2", "3"]
    assert post.urls == ["https://ddstore.mk/graphql"]
    assert post.allow_redirects == [False]
    assert post.headers[0]["User-Agent"] == DDSTORE_USER_AGENT
    variables = post.payloads[0]["variables"]
    assert isinstance(variables, dict)
    assert variables == {
        "search": "",
        "pageSize": 500,
        "currentPage": 1,
        "sort": {"name": "ASC"},
    }
    assert data.link == "https://ddstore.mk/products/product-1.html"
    assert data.timestamp == "2024-07-09T08:54:25+00:00"
    assert data.image_url == "https://ddstore.mk/media/1.webp"
    assert data.categories == ("Computers",)
    assert data.source_metrics == (
        SourceMetric(label="Price", value="1.499 ден."),
        SourceMetric(label="Original", value="1.999 ден."),
        SourceMetric(label="Stock", value="In stock"),
    )


def test_ddstore_strategy_maps_non_stock_status_to_out_of_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                catalog_payload(
                    1,
                    [product_payload("1", stock_status="OUT_OF_STOCK")],
                    current_page=1,
                ),
            ),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When
    entries, _ = DDStoreStrategy().fetch_entries(CATALOG_URL)
    data = DDStoreStrategy().get_entry_data(entries[0])

    # Then
    assert data.source_metrics[-1] == SourceMetric(label="Stock", value="Out of stock")


def test_ddstore_product_normalizes_user_visible_html_and_control_whitespace() -> None:
    # Given
    payload = product_payload("1")
    payload["name"] = "\t1.5&quot;\x00 Super\n AMOLED  "
    payload["url_key"] = "products/item&amp;1"
    categories = payload["categories"]
    assert isinstance(categories, list)
    category = categories[0]
    assert isinstance(category, dict)
    category["name"] = "  Displays\t&amp;\x1f Monitors\n"

    # When
    product = DDStoreProduct.model_validate(payload)

    # Then
    assert product.name == '1.5" Super AMOLED'
    assert product.categories is not None
    assert product.categories[0].name == "Displays & Monitors"
    assert product.url_key == "products/item&amp;1"


def test_ddstore_strategy_rejects_unknown_stock_status_as_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                catalog_payload(
                    1,
                    [product_payload("1", stock_status="PREORDER")],
                    current_page=1,
                ),
            ),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        DDStoreStrategy().fetch_entries(CATALOG_URL)


def test_ddstore_strategy_rejects_non_ddstore_https_origin() -> None:
    # Given
    strategy = DDStoreStrategy()

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        strategy.fetch_entries("https://ddstore.mk.evil.test/catalog")


def test_ddstore_strategy_rejects_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                b"",
                status_code=302,
                headers={"location": "https://127.0.0.1/internal"},
            ),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        DDStoreStrategy().fetch_entries(CATALOG_URL)
    assert len(post.urls) == 1


def test_ddstore_strategy_maps_graphql_errors_to_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost([StubResponse(b'{"errors": [{"message": "failure"}]}')])
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        DDStoreStrategy().fetch_entries(CATALOG_URL)


def test_ddstore_strategy_rejects_partial_graphql_response_with_data_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = catalog_payload(1, [product_payload("1")], current_page=1)
    partial_payload = response[:-1] + b', "errors": [{"message": "partial"}]}'
    monkeypatch.setattr(
        ddstore_http,
        "_create_session",
        lambda: RecordingPost([StubResponse(partial_payload)]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        DDStoreStrategy().fetch_entries(CATALOG_URL)


def test_ddstore_product_accepts_nullable_ancillary_display_metadata() -> None:
    # Given
    payload = product_payload("1")
    payload.pop("sku")
    payload["url_suffix"] = None
    payload["small_image"] = None
    payload["categories"] = None
    price_range = payload["price_range"]
    assert isinstance(price_range, dict)
    minimum_price = price_range["minimum_price"]
    assert isinstance(minimum_price, dict)
    minimum_price["regular_price"] = None

    # When
    product = DDStoreProduct.model_validate(payload)

    # Then
    assert product.small_image is None
    assert product.categories is None
    assert product.url_suffix is None
    assert product.price_range.minimum_price.regular_price is None


@pytest.mark.parametrize(
    ("final_currency", "regular_currency", "regular_price"),
    [("USD", "MKD", 1_999), ("MKD", "EUR", 1_999), ("MKD", "MKD", 999)],
)
def test_ddstore_product_rejects_non_mkd_or_inconsistent_prices(
    final_currency: str,
    regular_currency: str,
    regular_price: int,
) -> None:
    # Given
    payload = product_payload("1", price=1_499, regular_price=regular_price)
    price_range = payload["price_range"]
    assert isinstance(price_range, dict)
    minimum_price = price_range["minimum_price"]
    assert isinstance(minimum_price, dict)
    final = minimum_price["final_price"]
    regular = minimum_price["regular_price"]
    assert isinstance(final, dict)
    assert isinstance(regular, dict)
    final["currency"] = final_currency
    regular["currency"] = regular_currency

    # When / Then
    with pytest.raises(ValidationError):
        DDStoreProduct.model_validate(payload)


def test_ddstore_strategy_maps_malformed_required_name_to_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    payload = product_payload("1")
    payload["name"] = 1
    post = RecordingPost([StubResponse(catalog_payload(1, [payload], current_page=1))])
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        DDStoreStrategy().fetch_entries(CATALOG_URL)


@pytest.mark.parametrize(
    ("field", "value"),
    [("uid", "   "), ("name", "\x00\t\n"), ("url_key", "   ")],
)
def test_ddstore_strategy_rejects_required_fields_without_visible_text(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    # Given
    payload = product_payload("1")
    payload[field] = value
    post = RecordingPost([StubResponse(catalog_payload(1, [payload], current_page=1))])
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        DDStoreStrategy().fetch_entries(CATALOG_URL)


def test_ddstore_strategy_preserves_lowercase_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [StubResponse(b"rate limited", status_code=429, headers={"retry-after": "7"})],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError) as fetch_error:
        DDStoreStrategy().fetch_entries(CATALOG_URL)
    assert fetch_error.value.retry_after == 7


@pytest.mark.parametrize(
    "product_url",
    [
        "https://evil.example/products/1",
        "//evil.example/products/1",
        "products/../account",
    ],
)
def test_ddstore_product_rejects_unsafe_product_url(product_url: str) -> None:
    # Given
    payload = product_payload("1")
    payload["url_key"] = product_url

    # When / Then
    with pytest.raises(ValidationError):
        DDStoreProduct.model_validate(payload)


@pytest.mark.parametrize(
    "image_url",
    ["https://evil.example/product.webp", "https://images.ddstore.mk/product.webp"],
)
def test_ddstore_product_omits_unapproved_image_url(image_url: str) -> None:
    # Given
    payload = product_payload("1")
    image = payload["small_image"]
    assert isinstance(image, dict)
    image["url"] = image_url

    # When
    product = DDStoreProduct.model_validate(payload)

    # Then
    assert product.small_image is not None
    assert product.small_image.url is None
