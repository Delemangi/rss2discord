from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.transports import technomarket_catalog
from rss2discord.transports.technomarket_catalog import (
    TECHNOMARKET_FEED_URL,
    TechnomarketCatalogClient,
    parse_mkd_price,
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

    product = parse_product_card(document.select_one("li.product-fix"))

    assert product.product_id == "29404051"
    assert product.name.startswith("Notebook Acer Aspire Lite")
    assert product.url.startswith("https://tehnomarket.com.mk/product/29404051/")
    assert product.regular_price == 20999
    assert product.smart_price == 19499
    assert product.effective_price == 19499
    assert product.manufacturer == "ACER"
    assert (
        product.image_url
        == "https://d3mrte3vpewnxc.cloudfront.net/img/products/full/thumbs/nx.j9sex.001.jpg"
    )


def test_technomarket_parser_falls_back_to_regular_price_without_smart_price() -> None:
    document = BeautifulSoup(
        (FIXTURES / "category-page-1.html").read_text(encoding="utf-8"),
        "html.parser",
    )

    product = parse_product_card(document.select("li.product-fix")[1])

    assert product.smart_price is None
    assert product.regular_price == 22999
    assert product.effective_price == 22999


def test_technomarket_parses_comma_grouped_mkd_prices() -> None:
    assert parse_mkd_price("20,999 ден.") == 20999
    assert parse_mkd_price("19,499 ден.") == 19499


@pytest.mark.parametrize(
    "url",
    [
        "http://tehnomarket.com.mk/category/4003/laptopi",
        "https://user:password@tehnomarket.com.mk/category/4003/laptopi",
        "https://www.tehnomarket.com.mk/category/4003/laptopi",
        "https://tehnomarket.com.mk:443/category/4003/laptopi",
        "https://tehnomarket.com.mk/category/4003/laptopi?sort=price",
        "https://tehnomarket.com.mk/category/4003/laptopi#products",
        "https://tehnomarket.com.mk/products/4003/laptopi",
        "https://tehnomarket.com.mk/category/not-a-number/laptopi",
        "https://tehnomarket.com.mk/category/4003/laptopi/extra",
    ],
)
def test_technomarket_rejects_non_credential_free_category_roots(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        validate_technomarket_url(url)


def test_technomarket_catalog_fetches_complete_scope_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = (FIXTURES / "category-page-1.html").read_text(encoding="utf-8")
    first_page = first_page.replace("1 - 32 од 57 производи", "1 - 2 од 3 производи")
    responses = {
        TECHNOMARKET_FEED_URL: first_page,
        f"{TECHNOMARKET_FEED_URL}/page/2": (
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

    assert [product.product_id for product in products] == [
        "29404051",
        "29400351",
        "29400003",
    ]
    assert calls == [TECHNOMARKET_FEED_URL, f"{TECHNOMARKET_FEED_URL}/page/2"]


def test_technomarket_catalog_rejects_changed_page_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = (FIXTURES / "category-page-1.html").read_text(encoding="utf-8")
    first_page = first_page.replace("1 - 32 од 57 производи", "1 - 2 од 3 производи")
    second_page = (FIXTURES / "category-page-2.html").read_text(encoding="utf-8")
    second_page = second_page.replace("3 - 3 од 3 производи", "3 - 3 од 4 производи")
    responses = {
        TECHNOMARKET_FEED_URL: first_page,
        f"{TECHNOMARKET_FEED_URL}/page/2": second_page,
    }
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: responses[url]),
    )

    with pytest.raises(FeedFetchError, match="CatalogChanged"):
        TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)


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

    assert [product.product_id for product in products] == ["29404051", "29400351"]
    assert calls == [TECHNOMARKET_FEED_URL]


def test_technomarket_catalog_fails_closed_on_malformed_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(
            lambda url, **kwargs: (
                '<div class="products-range">not a count</div>'
                '<li class="product-fix"><a href="/product/1/item">Item</a></li>'
            ),
        ),
    )

    with pytest.raises(FeedFetchError, match="MalformedCount"):
        TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)


def test_technomarket_rejects_listing_with_missing_numeric_identity() -> None:
    card = BeautifulSoup(
        '<li class="product-fix"><div class="product-name"><a href="/product/no-code/item">Item</a></div></li>',
        "html.parser",
    ).select_one("li.product-fix")

    with pytest.raises(FeedFetchError, match="InvalidProductIdentity"):
        parse_product_card(card)


def test_technomarket_rejects_pagination_beyond_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(
            lambda url, **kwargs: (
                '<div class="products-range">1 - 32 од 3201 производи</div>'
                '<a href="/category/4003/laptopi/page/101">101</a>'
            ),
        ),
    )

    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)


class _Response:
    encoding = "utf-8"

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None


def test_technomarket_http_boundary_rejects_redirects_and_large_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def redirect(url: str, **kwargs: object) -> _Response:
        del url
        calls.append(kwargs)
        return _Response(302)

    monkeypatch.setattr(technomarket_catalog, "_perform_request", redirect)
    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        TechnomarketCatalogClient._fetch_html(TECHNOMARKET_FEED_URL)
    assert calls[0]["allow_redirects"] is False

    monkeypatch.setattr(technomarket_catalog, "MAX_TECHNOMARKET_RESPONSE_BYTES", 3)

    def oversized(url: str, **kwargs: object) -> _Response:
        del url
        callback = kwargs["content_callback"]
        assert callable(callback)
        callback(b"1234")
        return _Response()

    monkeypatch.setattr(technomarket_catalog, "_perform_request", oversized)
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        TechnomarketCatalogClient._fetch_html(TECHNOMARKET_FEED_URL)
