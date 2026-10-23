"""Reliability checks for CCCenter's documented public Store API client."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.cccenter import catalog as cccenter_catalog
from rss2discord.providers.cccenter.catalog import (
    CCCENTER_FEED_URL,
    CCCenterCatalogClient,
    parse_product_detail,
    parse_product_listing,
)
from rss2discord.providers.cccenter.models import CCCenterListing
from rss2discord.retries import FeedFetchInterruptedError


def _dto(number: int) -> dict[str, object]:
    slug = f"item-{number}"
    return {
        "id": number,
        "slug": slug,
        "permalink": f"https://cccenter.mk/product/{slug}/",
        "name": f"Item {number}",
        "sku": f"SKU-{number}",
        "images": [],
        "categories": [],
        "is_in_stock": number != 4,
        "type": "simple",
        "has_options": False,
        "prices": {
            "currency_code": "MKD",
            "currency_minor_unit": 2,
            "price": "125000",
            "regular_price": "125000",
            "price_range": None,
        },
    }


@dataclass
class _Response:
    status_code: int
    headers: Mapping[str, str]
    encoding: str = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError("unexpected fixture HTTP failure")


@dataclass
class _ApiTransport:
    total: int = 151
    calls: list[str] | None = None
    mutate_headers_on_page: int | None = None
    short_on_call: int | None = None
    duplicate_second_page: bool = False
    malformed_content_type: str = "application/json; charset=utf-8"
    shutdown: bool = False

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def request(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
        stream: bool,
        content_callback: Callable[[bytes], int],
    ) -> _Response:
        del headers, timeout, stream
        assert allow_redirects is False
        assert self.calls is not None
        self.calls.append(url)
        query = parse_qs(urlsplit(url).query)
        assert query["rest_route"] == ["/wc/store/v1/products"]
        page = int(query["page"][0])
        total = self.total
        if self.mutate_headers_on_page == page and len(self.calls) > 1:
            total += 1
        page_count = (total + 23) // 24
        start = (page - 1) * 24 + 1
        count = min(24, total - start + 1)
        if self.short_on_call == len(self.calls):
            count -= 1
        payload = [_dto(number) for number in range(start, start + max(0, count))]
        if self.duplicate_second_page and page == 2 and payload:
            payload[0] = _dto(1)
        content_callback(json.dumps(payload).encode())
        if len(self.calls) >= 2 and self.shutdown:
            self.shutdown = False
        return _Response(
            200,
            {
                "Content-Type": self.malformed_content_type,
                "X-WP-Total": str(total),
                "X-WP-TotalPages": str(page_count),
            },
        )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> _ApiTransport:
    transport = _ApiTransport()
    monkeypatch.setattr(cccenter_catalog, "_perform_request", transport.request)
    return transport


def test_complete_store_catalog_checks_all_pages_and_rechecks_root(
    api: _ApiTransport,
) -> None:
    products = CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)
    assert len(products) == 151
    assert len({product.product_id for product in products}) == 151
    assert [product.current_price for product in products] == [Decimal("1250.00")] * 151
    assert [
        int(parse_qs(urlsplit(url).query)["page"][0]) for url in api.calls or []
    ] == [
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        1,
    ]
    assert all("orderby=date" in url and "order=desc" in url for url in api.calls or [])


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"mutate_headers_on_page": 2}, "CatalogMetadataDrift"),
        ({"short_on_call": 2}, "IncompletePage"),
        ({"duplicate_second_page": True}, "DuplicateProduct"),
        ({"short_on_call": 8}, "IncompletePage"),
    ],
)
def test_api_drift_and_incomplete_catalog_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, object],
    error: str,
) -> None:
    transport = _ApiTransport()
    for field, value in updates.items():
        assert hasattr(transport, field)
        setattr(transport, field, value)
    monkeypatch.setattr(cccenter_catalog, "_perform_request", transport.request)
    with pytest.raises(FeedFetchError, match=error):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


@pytest.mark.parametrize(
    ("total", "error"),
    [
        (0, "InvalidResponse"),
        (2001, "ProductLimitExceeded"),
        (289, "PageLimitExceeded"),
    ],
)
def test_api_count_and_page_limits_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    total: int,
    error: str,
) -> None:
    transport = _ApiTransport(total=total)
    monkeypatch.setattr(cccenter_catalog, "_perform_request", transport.request)
    with pytest.raises(FeedFetchError, match=error):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


def test_api_requires_exact_json_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _ApiTransport(malformed_content_type="application/jsonp")
    monkeypatch.setattr(cccenter_catalog, "_perform_request", transport.request)
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


def test_detail_inputs_are_validated_before_any_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _ApiTransport()
    monkeypatch.setattr(cccenter_catalog, "_perform_request", transport.request)
    url = "https://cccenter.mk/product/item-1/"
    invalid = CCCenterListing("different", "Item 1", url, Decimal(1250), None, None)
    with pytest.raises(FeedFetchError, match="InvalidProductIdentity"):
        CCCenterCatalogClient().fetch_product_details((invalid,))
    assert transport.calls == []


def test_html_detail_helper_still_rejects_unrelated_prices() -> None:
    url = "https://cccenter.mk/product/item-1/"
    listing = CCCenterListing(url, "Item 1", url, Decimal(1250), None, None)
    document = BeautifulSoup(
        '<div class="related"><h1 class="product_title">Wrong</h1></div>',
        "html.parser",
    )
    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        cccenter_catalog.parse_product_detail(document, listing)


def _html_listing(number: int = 1) -> CCCenterListing:
    return parse_product_listing(
        BeautifulSoup(
            f'<li class="product"><a href="/product/item-{number}/">'
            f'<h2 class="woocommerce-loop-product__title">Item {number}</h2>'
            f'<img src="/item-{number}.jpg"><span class="price">1.250,00 ден</span>'
            "</a></li>",
            "html.parser",
        ).select_one("li.product"),
    )


def test_html_detail_ignores_sticky_cart_and_related_prices() -> None:
    html = """
        <div class="product product-type-variable"><p class="price">99.999 ден</p></div>
        <div class="etheme-sticky-cart"><p class="price">9.999 ден</p></div>
        <main class="product product-type-simple">
          <h1 class="product_title">Item 1</h1>
          <div class="summary"><p class="price">1.250,00 ден</p></div>
          <div class="related"><p class="price">8.888 ден</p></div>
        </main>
    """
    document = BeautifulSoup(html, "html.parser")
    before = str(document)
    result = parse_product_detail(document, _html_listing())
    assert result.price_status == "scalar"
    assert result.current_price == Decimal(1250)
    assert str(document) == before


@pytest.mark.parametrize(
    ("classes", "form"),
    [
        ("product product-type-variable", ""),
        (
            "product product-type-simple",
            '<form class="variations_form"><select name="attribute_size"></select></form>',
        ),
    ],
)
def test_html_detail_preserves_main_product_variable_evidence(
    classes: str,
    form: str,
) -> None:
    html = (
        f'<main class="{classes}"><h1 class="product_title">Item 1</h1>'
        '<div class="summary"><p class="price">1.250 ден</p>'
        f"{form}</div></main>"
    )
    assert (
        parse_product_detail(
            BeautifulSoup(html, "html.parser"),
            _html_listing(),
        ).price_status
        == "variable"
    )


def test_html_detail_refuses_ambiguous_prices_and_sidebar_borrowing() -> None:
    listing = _html_listing()
    with pytest.raises(FeedFetchError, match="AmbiguousPrice"):
        parse_product_detail(
            BeautifulSoup(
                '<h1 class="product_title">Item 1</h1><p class="price">10 ден</p><p class="price">20 ден</p>',
                "html.parser",
            ),
            listing,
        )
    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        parse_product_detail(
            BeautifulSoup(
                '<div class="unknown"><h1 class="product_title">Item 1</h1></div>'
                '<div class="sidebar"><p class="price">9.999 ден</p></div>',
                "html.parser",
            ),
            listing,
        )


def test_html_empty_variation_form_is_not_variable_evidence() -> None:
    html = (
        '<main class="product"><h1 class="product_title">Item 1</h1>'
        '<p class="price">1.250 ден</p><form class="variations_form"></form></main>'
    )
    assert (
        parse_product_detail(
            BeautifulSoup(html, "html.parser"),
            _html_listing(),
        ).price_status
        == "scalar"
    )


def test_shutdown_before_catalog_request_makes_no_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _ApiTransport()
    monkeypatch.setattr(cccenter_catalog, "_perform_request", transport.request)
    with pytest.raises(FeedFetchInterruptedError):
        CCCenterCatalogClient().fetch_catalog(
            CCCENTER_FEED_URL,
            is_shutdown_requested=lambda: True,
        )
    assert transport.calls == []
