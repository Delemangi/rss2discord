from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from rss2discord.models import SourceMetric
from rss2discord.providers.woocommerce import http as woocommerce_http
from rss2discord.providers.woocommerce.models import WooCommerceProduct
from rss2discord.providers.woocommerce.strategy import WooCommerceStrategy
from rss2discord.transports import FeedFetchError
from tests.woocommerce_helpers import (
    CATEGORY_URL,
    SHOP_URL,
    RecordingGet,
    StubResponse,
    categories_payload,
    category_payload,
    product_payload,
    products_payload,
)


def test_woocommerce_strategy_fetches_latest_products_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                products_payload([product_payload(3), product_payload(2)]),
                headers={"X-WP-Total": "2", "X-WP-TotalPages": "1"},
            ),
        ],
    )
    monkeypatch.setattr(woocommerce_http.requests, "get", get)
    strategy = WooCommerceStrategy()

    # When
    entries, source_title = strategy.fetch_entries(SHOP_URL)
    data = strategy.get_entry_data(entries[0])

    # Then
    assert source_title == "WooCommerce"
    assert strategy.seed_existing_on_first_fetch
    assert strategy.require_entries_for_initialization
    assert [strategy.get_entry_id(entry) for entry in entries] == ["2", "3"]
    assert data.title == "Product 2 – Gaming"
    assert data.link == "https://technosapien.mk/product/product-2/"
    assert data.timestamp is not None
    assert data.image_url == (
        "https://technosapien.mk/wp-content/uploads/product-2-300x300.jpg"
    )
    assert data.categories == ("Графички Карти",)
    assert data.source_metrics == (
        SourceMetric("Price", "1.499 ден."),
        SourceMetric("Original", "1.999 ден."),
        SourceMetric("Stock", "In stock"),
        SourceMetric("SKU", "SKU-2"),
    )
    query = parse_qs(urlsplit(get.urls[0]).query)
    assert query["per_page"] == ["30"]
    assert query["orderby"] == ["date"]
    assert query["order"] == ["desc"]
    assert query["_fields"] == [
        "id,name,permalink,sku,prices,images,categories,is_in_stock,type",
    ]


def test_woocommerce_strategy_resolves_category_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                categories_payload(
                    [category_payload(100, "SSD/HDD", "ssd-hdd")],
                ),
            ),
            StubResponse(
                products_payload([product_payload(1)]),
                headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"},
            ),
        ],
    )
    monkeypatch.setattr(woocommerce_http.requests, "get", get)

    # When
    entries, _source_title = WooCommerceStrategy().fetch_entries(CATEGORY_URL)

    # Then
    assert len(entries) == 1
    query = parse_qs(urlsplit(get.urls[1]).query)
    assert query["category"] == ["100"]


@pytest.mark.parametrize(
    "url",
    [
        "http://technosapien.mk/",
        "https://user:pass@technosapien.mk/",
        "https://technosapien.mk/product/product-1/",
        "https://technosapien.mk/?query=1",
        "https://127.0.0.1/",
        "https://localhost/",
    ],
)
def test_woocommerce_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        WooCommerceStrategy().fetch_entries(url)


def test_woocommerce_rejects_non_first_party_product_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                products_payload([product_payload(1, host="evil.example")]),
                headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"},
            ),
        ],
    )
    monkeypatch.setattr(woocommerce_http.requests, "get", get)

    with pytest.raises(FeedFetchError, match="InvalidProductUrl"):
        WooCommerceStrategy().fetch_entries(SHOP_URL)


def test_woocommerce_rejects_regular_price_below_current_price() -> None:
    with pytest.raises(ValidationError):
        WooCommerceProduct.model_validate(
            product_payload(1, price="200000", regular_price="100000"),
        )
