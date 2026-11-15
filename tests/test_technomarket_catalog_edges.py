"""Boundary and failure branches of the Technomarket catalog client."""

from collections.abc import Mapping
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.technomarket import catalog as technomarket_catalog
from rss2discord.providers.technomarket.catalog import (
    TECHNOMARKET_FEED_URL,
    TechnomarketCatalogClient,
    _decode_html,
    validate_technomarket_url,
)
from rss2discord.providers.technomarket.parser import (
    _page_count,
    _page_info,
    _page_number,
    _safe_image_url,
    _safe_product_url,
    parse_mkd_price,
    parse_product_card,
)
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.scan_budget import ScanBudget


def _card(
    *,
    data_id: str = "1",
    href: str = "/product/1/item",
    title: str = "Item",
    extra: str = "",
) -> str:
    return (
        f'<li class="product-fix" data-id="{data_id}">'
        f'<a href="{href}">{title}</a>'
        f'<div class="product-name">{title}</div>'
        f"{extra}</li>"
    )


def _page(*, count: str, cards: str, pagination: str = "") -> str:
    return f'<div class="products-range">{count}</div>{pagination}{cards}'


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        encoding: str | None = "utf-8",
    ) -> None:
        self.status_code = status_code
        self.headers: Mapping[str, str] = headers or {}
        self.encoding = encoding

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise curl_requests.exceptions.HTTPError(str(self.status_code))


def test_technomarket_perform_request_passes_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> object:
        calls["url"] = url
        calls.update(kwargs)
        return object()

    def callback(chunk: bytes) -> int:
        return len(chunk)

    monkeypatch.setattr(technomarket_catalog.curl_requests, "get", fake_get)
    technomarket_catalog._perform_request(
        TECHNOMARKET_FEED_URL,
        headers={"Accept": "text/html"},
        timeout=5.0,
        allow_redirects=False,
        stream=False,
        content_callback=callback,
    )

    assert calls["url"] == TECHNOMARKET_FEED_URL
    assert calls["allow_redirects"] is False
    assert calls["stream"] is False
    assert calls["content_callback"] is callback


def test_technomarket_rejects_unparseable_port() -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        validate_technomarket_url(
            "https://tehnomarket.com.mk:bad/category/4003/laptopi",
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("no price", None),
        ("20,99 ден.", Decimal("20.99")),
        ("1,234,567 ден.", Decimal(1234567)),
        ("1.234", Decimal(1234)),
        ("1.234,56 ден.", Decimal("1234.56")),
        ("999999999999999999999", None),
    ],
)
def test_technomarket_price_parsing_edges(
    value: str,
    expected: Decimal | None,
) -> None:
    assert parse_mkd_price(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://[::1/product/1/item",
        "https://tehnomarket.com.mk:bad/product/1/item",
        "https://example.com/product/1/item",
        "http://tehnomarket.com.mk/product/1/item",
    ],
)
def test_technomarket_product_url_rejects_invalid_targets(value: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidProductUrl"):
        _safe_product_url(value)


def test_technomarket_product_url_rejects_ambiguous_identity() -> None:
    with pytest.raises(FeedFetchError, match="InvalidProductIdentity"):
        _safe_product_url("/product/no-code/item")


def test_technomarket_product_url_extracts_numeric_token_from_slug() -> None:
    url, product_id = _safe_product_url("/product/notebook-29404051/")

    assert product_id == "29404051"
    assert url == "https://tehnomarket.com.mk/product/notebook-29404051/"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://[::1/image.jpg",
        "https://tehnomarket.com.mk:bad/image.jpg",
        "http://example.com/image.jpg",
        "https://user:pass@example.com/image.jpg",
        "https://example.com:8443/image.jpg",
    ],
)
def test_technomarket_image_url_rejects_invalid_values(value: str | None) -> None:
    assert _safe_image_url(value) is None


def test_technomarket_image_url_keeps_query() -> None:
    assert (
        _safe_image_url("https://cdn.example.com/image.jpg?x=1")
        == "https://cdn.example.com/image.jpg?x=1"
    )


def test_technomarket_product_card_requires_a_card() -> None:
    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        parse_product_card(None)


def test_technomarket_product_card_requires_link_and_title() -> None:
    card = BeautifulSoup(
        '<li class="product-fix" data-id="1"><a href="/product/1/item">Item</a></li>',
        "html.parser",
    ).select_one("li.product-fix")

    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        parse_product_card(card)


def test_technomarket_product_card_rejects_identity_mismatch() -> None:
    card = BeautifulSoup(
        _card(data_id="2", href="/product/1/item"),
        "html.parser",
    ).select_one("li.product-fix")

    with pytest.raises(FeedFetchError, match="InvalidProductIdentity"):
        parse_product_card(card)


def test_technomarket_product_card_reads_brand_and_image() -> None:
    card = BeautifulSoup(
        _card(
            extra=(
                '<span class="brand">ACME</span>'
                '<img class="product-image" src="https://cdn.example.com/item.jpg">'
            ),
        ),
        "html.parser",
    ).select_one("li.product-fix")

    product = parse_product_card(card)

    assert product.manufacturer == "ACME"
    assert product.image_url == "https://cdn.example.com/item.jpg"


def test_technomarket_page_info_requires_a_range() -> None:
    document = BeautifulSoup("<html></html>", "html.parser")

    with pytest.raises(FeedFetchError, match="MalformedCount"):
        _page_info(document)


@pytest.mark.parametrize(
    "count",
    [
        "0 - 2 од 3 производи",
        "3 - 2 од 3 производи",
        "1 - 5 од 3 производи",
    ],
)
def test_technomarket_page_info_rejects_invalid_ranges(count: str) -> None:
    document = BeautifulSoup(
        f'<div class="products-range">{count}</div>',
        "html.parser",
    )

    with pytest.raises(FeedFetchError, match="MalformedCount"):
        _page_info(document)


def test_technomarket_page_count_rejects_zero_page() -> None:
    document = BeautifulSoup(
        '<a href="/category/4003/laptopi/page/0">0</a>',
        "html.parser",
    )

    with pytest.raises(FeedFetchError, match="MalformedPagination"):
        _page_count(document)


@pytest.mark.parametrize(
    "href",
    [
        "https://[::1/page/2",
        "https://tehnomarket.com.mk:bad/category/4003/laptopi/page/2",
        "/category/4003/laptopi/page/not-a-number",
        "http://tehnomarket.com.mk/category/4003/laptopi/page/2",
        "https://tehnomarket.com.mk/category/4003/laptopi/page/2?x=1",
        "https://tehnomarket.com.mk/category/4003/laptopi/page/2#x",
    ],
)
def test_technomarket_page_number_rejects_malformed_hrefs(href: str) -> None:
    with pytest.raises(FeedFetchError, match="MalformedPagination"):
        _page_number(href)


def test_technomarket_catalog_retries_retryable_catalog_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = _page(
        count="1 - 2 од 3 производи",
        cards=_card() + _card(data_id="2", href="/product/2/item"),
    )
    complete = _page(count="1 - 1 од 1 производи", cards=_card())
    pages = iter((incomplete, complete))
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: next(pages)),
    )
    sleeps: list[float] = []

    def sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        return True

    policy = FetchRetryPolicy(
        sleep=sleep,
        on_retry=lambda error, delay: None,
    )

    products = TechnomarketCatalogClient().fetch_catalog(
        TECHNOMARKET_FEED_URL,
        retry_policy=policy,
    )

    assert [product.product_id for product in products] == ["1"]
    assert len(sleeps) == 1


def test_technomarket_catalog_rejects_total_above_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page(count="1 - 2 од 3 производи", cards=_card())
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: page),
    )
    monkeypatch.setattr(technomarket_catalog, "MAX_TECHNOMARKET_PRODUCTS", 2)

    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)


def test_technomarket_catalog_rejects_incomplete_final_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page(
        count="1 - 2 од 3 производи",
        cards=_card() + _card(data_id="2", href="/product/2/item"),
    )
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: page),
    )

    with pytest.raises(FeedFetchError, match="IncompleteCatalog"):
        TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)


def test_technomarket_catalog_rejects_duplicate_product_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pagination = '<a href="/category/4003/laptopi/page/2">2</a>'
    first = _page(
        count="1 - 1 од 2 производи",
        cards=_card(),
        pagination=pagination,
    )
    second = _page(
        count="2 - 2 од 2 производи",
        cards=_card(),
        pagination=pagination,
    )
    responses = {
        TECHNOMARKET_FEED_URL: first,
        f"{TECHNOMARKET_FEED_URL}?page=2": second,
    }
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: responses[url]),
    )

    with pytest.raises(FeedFetchError, match="DuplicateProduct"):
        TechnomarketCatalogClient().fetch_catalog(TECHNOMARKET_FEED_URL)


def test_technomarket_parse_products_rejects_empty_page() -> None:
    document = BeautifulSoup(
        '<div class="products-range">1 - 1 од 1 производи</div>',
        "html.parser",
    )

    with pytest.raises(FeedFetchError, match="EmptyPage"):
        TechnomarketCatalogClient._parse_products(document)


def test_technomarket_fetch_html_tracks_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        technomarket_catalog,
        "_perform_request",
        lambda url, **kwargs: _Response(),
    )
    budget = ScanBudget.start(
        technomarket_catalog.TECHNOMARKET_SCAN_LIMITS,
        lambda: False,
    )
    after_calls: list[bool] = []
    original_after = ScanBudget.after_request

    def after_request(self: ScanBudget) -> None:
        after_calls.append(True)
        original_after(self)

    monkeypatch.setattr(ScanBudget, "after_request", after_request)

    html = TechnomarketCatalogClient._fetch_html(
        TECHNOMARKET_FEED_URL,
        budget=budget,
    )

    assert html == ""
    assert budget.requests == 1
    assert after_calls == [True]


def test_technomarket_request_html_reraises_feed_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = FeedFetchError("Technomarket", "InvalidResponse")
    monkeypatch.setattr(
        technomarket_catalog,
        "_perform_request",
        lambda url, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(FeedFetchError) as caught:
        TechnomarketCatalogClient._fetch_html(TECHNOMARKET_FEED_URL)

    assert caught.value is error


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (curl_requests.exceptions.RequestException("down"), "RequestException"),
        (ValueError("bad"), "ValueError"),
    ],
)
def test_technomarket_request_html_wraps_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    cause: str,
) -> None:
    monkeypatch.setattr(
        technomarket_catalog,
        "_perform_request",
        lambda url, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(FeedFetchError) as caught:
        TechnomarketCatalogClient._fetch_html(TECHNOMARKET_FEED_URL)

    assert caught.value.cause_type == cause


def test_technomarket_request_html_prefers_callback_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(technomarket_catalog, "MAX_TECHNOMARKET_RESPONSE_BYTES", 3)

    def oversized(url: str, **kwargs: object) -> _Response:
        callback = kwargs["content_callback"]
        assert callable(callback)
        callback(b"1234")
        raise curl_requests.exceptions.RequestException("down")

    monkeypatch.setattr(technomarket_catalog, "_perform_request", oversized)

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        TechnomarketCatalogClient._fetch_html(TECHNOMARKET_FEED_URL)


def test_technomarket_decode_html_reports_http_errors() -> None:
    response = _Response(503, {"Retry-After": "2.5"})

    with pytest.raises(FeedFetchError) as caught:
        _decode_html(response, bytearray(b"busy"))

    assert caught.value.cause_type == "HTTPError"
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert caught.value.retry_after == 2.5


@pytest.mark.parametrize(
    "value",
    ["abc", str(technomarket_catalog.MAX_TECHNOMARKET_RESPONSE_BYTES + 1)],
)
def test_technomarket_decode_html_rejects_invalid_content_length(value: str) -> None:
    response = _Response(headers={"Content-Length": value})

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        _decode_html(response, bytearray(b"x"))


def test_technomarket_decode_html_rejects_invalid_encoding() -> None:
    response = _Response(encoding="utf-8")

    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        _decode_html(response, bytearray(b"\xff"))


def test_technomarket_decode_html_falls_back_to_utf8() -> None:
    response = _Response(encoding=None)

    assert _decode_html(response, bytearray(b"text")) == "text"
