from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.providers.cccenter import catalog as cccenter_catalog
from rss2discord.providers.cccenter.catalog import (
    CCCenterCatalogClient,
    parse_mkd_price,
    parse_product_detail,
    parse_product_listing,
    validate_cccenter_url,
)
from rss2discord.providers.cccenter.models import CCCenterProduct
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import FeedFetchError

FIXTURES = Path(__file__).parent / "fixtures" / "cccenter"


def test_cccenter_parses_listing_sale_price_and_product_detail() -> None:
    listing = (FIXTURES / "listing.html").read_text(encoding="utf-8")
    detail = (FIXTURES / "product-alpha.html").read_text(encoding="utf-8")

    card = parse_product_listing(
        BeautifulSoup(listing, "html.parser").select_one("li.product"),
    )
    product = parse_product_detail(
        BeautifulSoup(detail, "html.parser"),
        card,
    )

    assert parse_mkd_price("56.000,00 ден") == 56000
    assert product.product_id == "https://cccenter.mk/product/alpha-laptop/"
    assert product.current_price == 56000
    assert product.original_price == 59000
    assert product.sku == "ALPHA-001"
    assert product.categories == ("Лаптопи",)
    assert product.image_url is not None
    assert product.image_url.endswith("alpha.jpg")
    assert product.published_at is None


def test_cccenter_treats_missing_stock_marker_as_in_stock() -> None:
    listing = parse_product_listing(
        BeautifulSoup(
            '<li class="product"><a href="/product/alpha/"><img src="/alpha.jpg"><h2 class="woocommerce-loop-product__title">Alpha</h2></a></li>',
            "html.parser",
        ).select_one("li.product"),
    )

    product = parse_product_detail(
        BeautifulSoup('<h1 class="product_title">Alpha</h1>', "html.parser"),
        listing,
    )

    assert product.is_in_stock is True


@pytest.mark.parametrize(
    ("detail_stock_markup", "expected_stock"),
    [
        ("", False),
        ('<p class="stock in-stock">In stock</p>', False),
        ('<p class="stock out-of-stock">Out of stock</p>', False),
    ],
)
def test_cccenter_detail_does_not_restore_listing_out_of_stock(
    detail_stock_markup: str,
    expected_stock: bool,
) -> None:
    listing = CCCenterProduct(
        product_id="https://cccenter.mk/product/alpha/",
        name="Alpha",
        url="https://cccenter.mk/product/alpha/",
        sku="",
        current_price=None,
        original_price=None,
        image_url=None,
        categories=(),
        is_in_stock=False,
    )

    product = parse_product_detail(
        BeautifulSoup(
            f'<h1 class="product_title">Alpha</h1>{detail_stock_markup}',
            "html.parser",
        ),
        listing,
    )

    assert product.is_in_stock is expected_stock


def test_cccenter_detail_out_of_stock_marker_overrides_in_stock_listing() -> None:
    listing = parse_product_listing(
        BeautifulSoup(
            '<li class="product"><a href="/product/alpha/"><img src="/alpha.jpg">'
            '<h2 class="woocommerce-loop-product__title">Alpha</h2></a></li>',
            "html.parser",
        ).select_one("li.product"),
    )

    product = parse_product_detail(
        BeautifulSoup(
            '<h1 class="product_title">Alpha</h1>'
            '<p class="stock out-of-stock">Out of stock</p>',
            "html.parser",
        ),
        listing,
    )

    assert product.is_in_stock is False


@pytest.mark.parametrize(
    "url",
    [
        "http://cccenter.mk/shop/?orderby=date",
        "https://www.cccenter.mk/shop/?orderby=date",
        "https://cccenter.mk:443/shop/?orderby=date",
        "https://cccenter.mk/shop/",
        "https://cccenter.mk/shop/?orderby=price",
        "https://cccenter.mk/shop/?orderby=date&extra=1",
        "https://cccenter.mk/shop/?orderby=%64ate",
    ],
)
def test_cccenter_rejects_noncanonical_feed_urls(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        validate_cccenter_url(url)


def test_cccenter_rejects_listing_cards_without_an_image() -> None:
    card = BeautifulSoup(
        '<li class="product"><a href="/product/alpha/"><h2 class="woocommerce-loop-product__title">Alpha</h2></a></li>',
        "html.parser",
    ).select_one("li.product")

    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        parse_product_listing(card)


@pytest.mark.parametrize(
    "product_url",
    [
        "https://cccenter.mk/product/alpha",
        "https://cccenter.mk/product/alpha//",
        "https://cccenter.mk/product/alpha/../beta/",
        "https://cccenter.mk/product/alpha%2Fbeta/",
        "https://cccenter.mk/product/./alpha/",
    ],
)
def test_cccenter_rejects_noncanonical_product_url_variants(product_url: str) -> None:
    card = BeautifulSoup(
        f'<li class="product"><a href="{product_url}"><h2 class="woocommerce-loop-product__title">Alpha</h2></a></li>',
        "html.parser",
    ).select_one("li.product")

    with pytest.raises(FeedFetchError, match="InvalidProductUrl"):
        parse_product_listing(card)


def test_cccenter_preserves_realistic_percent_encoded_product_identity() -> None:
    product_url = (
        "https://cccenter.mk/product/ssd-2-5%e2%80%b3-500gb-samsung-870-evo-retail/"
    )
    listing = parse_product_listing(
        BeautifulSoup(
            f'<li class="product"><a href="{product_url}">'
            '<img src="/ssd.jpg">'
            '<h2 class="woocommerce-loop-product__title">Samsung 870 EVO</h2>'
            '<span class="price">5.000 ден</span></a></li>',
            "html.parser",
        ).select_one("li.product"),
    )

    product = parse_product_detail(
        BeautifulSoup('<h1 class="product_title">Samsung 870 EVO</h1>', "html.parser"),
        listing,
    )

    assert listing.product_id == product_url
    assert listing.url == product_url
    assert product.product_id == product_url
    assert product.url == product_url


def _store_item(
    *,
    url: str = "https://cccenter.mk/product/alpha/",
    price: str = "740000",
    price_range: object = None,
) -> dict[str, object]:
    return {
        "id": 81,
        "slug": "alpha",
        "permalink": url,
        "name": "Alpha &amp; Beta",
        "sku": "A-1",
        "images": [],
        "categories": [],
        "is_in_stock": True,
        "type": "simple",
        "has_options": False,
        "prices": {
            "currency_code": "MKD",
            "currency_minor_unit": 2,
            "price": price,
            "regular_price": "800000",
            "price_range": price_range,
        },
    }


def test_store_api_preserves_permalink_identity_and_minor_units() -> None:
    seen: set[tuple[str, str | int]] = set()
    product = CCCenterCatalogClient._store_product(_store_item(), seen)
    assert product.product_id == "https://cccenter.mk/product/alpha/"
    assert product.current_price == 7400
    assert product.original_price == 8000
    assert product.name == "Alpha & Beta"


def test_store_api_encoded_permalink_identity_is_not_reconstructed() -> None:
    url = "https://cccenter.mk/product/ssd-2-5%e2%80%b3-500gb/"
    item = _store_item(url=url)
    item["slug"] = "ssd-2-5″-500gb"
    product = CCCenterCatalogClient._store_product(item, set())
    assert product.product_id == url


@pytest.mark.parametrize("price", ["-1", "1.2", "1e3", 1, True])
def test_store_api_rejects_noncanonical_minor_price(price: object) -> None:
    with pytest.raises(FeedFetchError, match="MalformedPrice"):
        cccenter_catalog._minor_amount(price)


def test_store_api_rejects_huge_minor_amount_as_typed_error() -> None:
    with pytest.raises(FeedFetchError, match="MalformedPrice"):
        cccenter_catalog._minor_amount("9" * 5000)


def test_store_api_preserves_range_and_zero_amount_status() -> None:
    assert (
        cccenter_catalog._store_price(_store_item(price_range={"min_price": "1"}))[2]
        == "range"
    )
    assert cccenter_catalog._minor_amount("0") == 0
    assert cccenter_catalog._minor_amount("1") == Decimal("0.01")


def test_store_api_rejects_bad_currency_even_when_product_is_variable() -> None:
    item = _store_item()
    item["type"] = "variable"
    prices = item["prices"]
    assert isinstance(prices, dict)
    prices["currency_code"] = "USD"
    with pytest.raises(FeedFetchError, match="InvalidCurrency"):
        cccenter_catalog._store_price(item)


def test_store_api_defers_observed_woosb_bundle_price_without_losing_identity() -> None:
    item = _store_item()
    item["type"] = "woosb"
    product = CCCenterCatalogClient._store_product(item, set())
    assert product.product_id == "https://cccenter.mk/product/alpha/"
    assert product.current_price is None
    assert product.original_price is None
    assert product.price_status == "variable"


def test_store_api_rejects_unrecognized_custom_product_type() -> None:
    item = _store_item()
    item["type"] = "unknown-custom-type"
    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        CCCenterCatalogClient._store_product(item, set())


def test_store_api_rejects_non_boolean_stock_and_permalink_slug_conflict() -> None:
    malformed = _store_item()
    malformed["is_in_stock"] = "false"
    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        CCCenterCatalogClient._store_product(malformed, set())
    conflict = _store_item()
    conflict["slug"] = "different"
    with pytest.raises(FeedFetchError, match="InvalidProductIdentity"):
        CCCenterCatalogClient._store_product(conflict, set())


@pytest.mark.parametrize(
    "product_url",
    [
        "/product/alpha/../beta/",
        "/product/alpha/./beta/",
        "/product/alpha//beta/",
        "/product/alpha%2fbeta/",
        "/product/alpha%2Fbeta/",
        "/product/alpha%5cbeta/",
        "/product/alpha%5CBeta/",
        "/product/alpha%2e%2ebeta/",
        "/product/alpha%2E%2ebeta/",
        "/product/alpha%252f/beta/",
        "/product/alpha%zz/",
        "/product/alpha%e2%80%zz/",
        "/product/alpha%e2%80%/",
        "/product/alpha%c0%af/",
        "/product/alpha%e2%28%a1/",
        "/product/alpha\x00/",
        "/product/alpha\n/",
        "/product/alpha /",
        "http://cccenter.mk/product/alpha/",
        "https://evil.example/product/alpha/",
        "https://cccenter.mk:443/product/alpha/",
        "https://user:secret@cccenter.mk/product/alpha/",
        "/product/alpha/?source=feed",
        "/product/alpha/#fragment",
    ],
)
def test_cccenter_rejects_unsafe_raw_product_paths(product_url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidProductUrl"):
        cccenter_catalog._safe_product_url(product_url)


def test_cccenter_marks_variable_and_range_prices_unavailable() -> None:
    variable = BeautifulSoup(
        '<li class="product product-type-variable"><a href="/product/variable/"><img src="/variable.jpg"><h2 class="woocommerce-loop-product__title">Variable</h2><span class="price">56.000,00 ден – 60.000,00 ден</span></a></li>',
        "html.parser",
    ).select_one("li.product")

    product = parse_product_listing(variable)

    assert product.current_price is None
    assert product.price_status == "variable"


def test_cccenter_treats_out_of_bounds_money_as_unpriced() -> None:
    assert parse_mkd_price("9999999999999,00 ден") is None


@pytest.mark.parametrize(
    ("listing_markup", "detail_markup", "expected_status"),
    [
        (
            '<li class="product"><a href="/product/merge/"><img src="/merge.jpg"><h2 class="woocommerce-loop-product__title">Merge</h2><span class="price">56.000,00 ден</span></a></li>',
            '<h1 class="product_title">Merge</h1><form class="variations_form"><select name="attribute_size"><option>Large</option></select><p class="price">56.000,00 ден</p></form>',
            "variable",
        ),
        (
            '<li class="product product-type-variable"><a href="/product/merge/"><img src="/merge.jpg"><h2 class="woocommerce-loop-product__title">Merge</h2><span class="price">56.000,00 ден</span></a></li>',
            '<h1 class="product_title">Merge</h1><p class="price">56.000,00 ден</p>',
            "variable",
        ),
        (
            '<li class="product"><a href="/product/merge/"><img src="/merge.jpg"><h2 class="woocommerce-loop-product__title">Merge</h2><span class="price">56.000,00 ден – 60.000,00 ден</span></a></li>',
            '<h1 class="product_title">Merge</h1><p class="price">56.000,00 ден</p>',
            "range",
        ),
        (
            '<li class="product"><a href="/product/merge/"><img src="/merge.jpg"><h2 class="woocommerce-loop-product__title">Merge</h2><span class="price">56.000,00 ден</span></a></li>',
            '<h1 class="product_title">Merge</h1>',
            "unpriced",
        ),
    ],
)
def test_cccenter_detail_price_status_preserves_listing_non_scalar_status(
    listing_markup: str,
    detail_markup: str,
    expected_status: str,
) -> None:
    listing = parse_product_listing(
        BeautifulSoup(listing_markup, "html.parser").select_one("li.product"),
    )

    product = parse_product_detail(BeautifulSoup(detail_markup, "html.parser"), listing)

    assert product.price_status == expected_status


class _StreamResponse:
    status_code = 200
    encoding = "utf-8"

    def __init__(self, chunks: list[bytes], status_code: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        if status_code in {301, 302}:
            self.headers = {"Location": "https://evil.example/"}

    def __enter__(self) -> "_StreamResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> list[bytes]:
        del chunk_size
        return self._chunks


def test_cccenter_rejects_redirects_without_following_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def perform(url: str, **kwargs: object) -> _StreamResponse:
        del url
        calls.append(kwargs)
        return _StreamResponse([], status_code=302)

    monkeypatch.setattr(cccenter_catalog, "_perform_request", perform)

    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        CCCenterCatalogClient._fetch_json(
            "https://cccenter.mk/?rest_route=/wc/store/v1/products",
            cccenter_catalog._ScanBudget.start(lambda: False),
        )
    assert calls[0]["allow_redirects"] is False


def test_cccenter_reads_responses_as_bounded_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_RESPONSE_BYTES", 3)

    def perform(url: str, **kwargs: object) -> _StreamResponse:
        del url
        callback = kwargs["content_callback"]
        assert callable(callback)
        callback(b"12")
        callback(b"34")
        return _StreamResponse([])

    monkeypatch.setattr(
        cccenter_catalog,
        "_perform_request",
        perform,
    )

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        CCCenterCatalogClient._fetch_json(
            "https://cccenter.mk/?rest_route=/wc/store/v1/products",
            cccenter_catalog._ScanBudget.start(lambda: False),
        )


@pytest.mark.parametrize(
    ("response_limit", "expected_cause", "request_raises"),
    [
        (3, "ResponseTooLarge", False),
        (3, "ResponseTooLarge", True),
        (None, "ScanResponseTooLarge", False),
        (None, "ScanResponseTooLarge", True),
    ],
)
def test_cccenter_propagates_typed_callback_abort(
    monkeypatch: pytest.MonkeyPatch,
    response_limit: int | None,
    expected_cause: str,
    request_raises: bool,
) -> None:
    if response_limit is not None:
        monkeypatch.setattr(
            cccenter_catalog,
            "MAX_CCCENTER_RESPONSE_BYTES",
            response_limit,
        )
    else:
        monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_SCAN_BYTES", 3)

    def perform(url: str, **kwargs: object) -> _StreamResponse:
        del url
        callback = kwargs["content_callback"]
        assert callable(callback)
        assert callback(b"12") == 2
        assert callback(b"34") == CURL_WRITEFUNC_ERROR
        if request_raises:
            raise curl_requests.exceptions.RequestException("write aborted")
        return _StreamResponse([])

    monkeypatch.setattr(cccenter_catalog, "_perform_request", perform)
    budget = cccenter_catalog._ScanBudget.start(lambda: False)

    with pytest.raises(FeedFetchError) as error:
        CCCenterCatalogClient._fetch_json(
            "https://cccenter.mk/?rest_route=/wc/store/v1/products",
            budget,
        )

    assert error.value.cause_type == expected_cause


def test_cccenter_propagates_shutdown_from_callback_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = False

    def is_shutdown_requested() -> bool:
        return shutdown

    def perform(url: str, **kwargs: object) -> _StreamResponse:
        nonlocal shutdown
        del url
        shutdown = True
        callback = kwargs["content_callback"]
        assert callable(callback)
        assert callback(b"12") == CURL_WRITEFUNC_ERROR
        raise curl_requests.exceptions.RequestException("write aborted")

    monkeypatch.setattr(cccenter_catalog, "_perform_request", perform)
    budget = cccenter_catalog._ScanBudget.start(is_shutdown_requested)

    with pytest.raises(FeedFetchInterruptedError):
        CCCenterCatalogClient._fetch_json(
            "https://cccenter.mk/?rest_route=/wc/store/v1/products",
            budget,
        )


def test_cccenter_checks_deadline_before_each_stream_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.0, 0.0, 0.0, 0.0, 0.0, 301.0))
    monkeypatch.setattr(cccenter_catalog, "monotonic", lambda: next(clock))

    calls: list[dict[str, object]] = []
    chunks = 0

    def perform(url: str, **kwargs: object) -> _StreamResponse:
        nonlocal chunks
        del url
        calls.append(kwargs)
        callback = kwargs["content_callback"]
        assert callable(callback)
        result = callback(b"x")
        if result == CURL_WRITEFUNC_ERROR:
            return _StreamResponse([])
        chunks += 1
        result = callback(b"x")
        if result == CURL_WRITEFUNC_ERROR:
            return _StreamResponse([])
        chunks += 1
        return _StreamResponse([])

    monkeypatch.setattr(cccenter_catalog, "_perform_request", perform)
    budget = cccenter_catalog._ScanBudget.start(lambda: False)

    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        CCCenterCatalogClient._fetch_json(
            "https://cccenter.mk/?rest_route=/wc/store/v1/products",
            budget,
        )
    assert chunks == 1
    assert calls[0]["allow_redirects"] is False
    assert calls[0]["stream"] is False
    assert calls[0]["timeout"] == 300.0


def test_cccenter_price_status_rejects_range_evidence_in_discount_markup() -> None:
    card = BeautifulSoup(
        '<li class="product"><a href="/product/conflict/"><img src="/conflict.jpg"><h2 class="woocommerce-loop-product__title">Conflict</h2><span class="price"><del>50.000,00 ден – 60.000,00 ден</del><ins>56.000,00 ден</ins></span></a></li>',
        "html.parser",
    ).select_one("li.product")

    product = parse_product_listing(card)

    assert product.current_price == 56000
    assert product.price_status == "range"
