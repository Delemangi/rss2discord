from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.transports.technomarket_catalog import (
    TECHNOMARKET_FEED_URL,
    TechnomarketCatalogClient,
    parse_product_card,
    validate_technomarket_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "technomarket"


def test_technomarket_accepts_canonical_category_root() -> None:
    assert validate_technomarket_url(TECHNOMARKET_FEED_URL) == TECHNOMARKET_FEED_URL


def test_technomarket_parser_extracts_identity_metadata_and_smart_price() -> None:
    document = BeautifulSoup(
        (FIXTURES / "category-page-1.html").read_text(encoding="utf-8"),
        "html.parser",
    )

    product = parse_product_card(document.select_one(".product-card"))

    assert product.product_id == "12345"
    assert product.name == "Alpha Laptop"
    assert product.url == "https://tehnomarket.com.mk/product/alpha-laptop-12345/"
    assert product.regular_price == 59999
    assert product.smart_price == 56999
    assert product.effective_price == 56999
    assert product.manufacturer == "AlphaTech"
    assert product.categories == ("Лаптопи",)
    assert product.image_url == "https://tehnomarket.com.mk/media/alpha.jpg"


def test_technomarket_parser_falls_back_to_regular_price_without_smart_price() -> None:
    document = BeautifulSoup(
        (FIXTURES / "category-page-1.html").read_text(encoding="utf-8"),
        "html.parser",
    )

    product = parse_product_card(document.select(".product-card")[1])

    assert product.smart_price is None
    assert product.effective_price == 24000


@pytest.mark.parametrize(
    "url",
    [
        "http://tehnomarket.com.mk/category/42/laptops",
        "https://www.tehnomarket.com.mk/category/42/laptops",
        "https://tehnomarket.com.mk:443/category/42/laptops",
        "https://tehnomarket.com.mk/category/42/laptops?sort=price",
        "https://tehnomarket.com.mk/category/42/laptops#products",
        "https://tehnomarket.com.mk/products/42/laptops",
        "https://tehnomarket.com.mk/category/not-a-number/laptops",
        "https://tehnomarket.com.mk/category/42/laptops/extra",
    ],
)
def test_technomarket_rejects_non_credential_free_category_roots(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        validate_technomarket_url(url)


def test_technomarket_catalog_fetches_complete_scope_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        TECHNOMARKET_FEED_URL: (FIXTURES / "category-page-1.html").read_text(
            encoding="utf-8",
        ),
        f"{TECHNOMARKET_FEED_URL}?page=2": (
            FIXTURES / "category-page-2.html"
        ).read_text(encoding="utf-8"),
    }
    calls: list[str] = []

    def fetch(url: str, **kwargs: object) -> str:
        del kwargs
        calls.append(url)
        return responses[url]

    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(fetch),
    )

    products = TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)

    assert [product.product_id for product in products] == ["12345", "67890", "24680"]
    assert calls == [TECHNOMARKET_FEED_URL, f"{TECHNOMARKET_FEED_URL}?page=2"]


def test_technomarket_discovery_reads_only_the_first_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = (FIXTURES / "category-page-1.html").read_text(encoding="utf-8")
    calls: list[str] = []

    def fetch(url: str, **kwargs: object) -> str:
        del kwargs
        calls.append(url)
        return first_page

    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(fetch),
    )

    products = TechnomarketCatalogClient().fetch_latest_products(TECHNOMARKET_FEED_URL)

    assert [product.product_id for product in products] == ["12345", "67890"]
    assert calls == [TECHNOMARKET_FEED_URL]


def test_technomarket_catalog_fails_closed_on_malformed_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: '<main data-total-pages="x"></main>'),
    )

    with pytest.raises(FeedFetchError, match="MalformedPagination"):
        TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)
