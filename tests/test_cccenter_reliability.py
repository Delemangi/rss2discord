from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.cccenter import catalog as cccenter_catalog
from rss2discord.providers.cccenter.bounds import MAX_CCCENTER_PAGES
from rss2discord.providers.cccenter.catalog import (
    CCCENTER_FEED_URL,
    CCCenterCatalogClient,
    parse_product_detail,
    parse_product_listing,
)
from rss2discord.retries import FeedFetchInterruptedError


def _card(number: int, *, price: str = "1.250,00 ден", classes: str = "") -> str:
    return (
        f'<li class="product {classes}"><a href="/product/item-{number}/">'
        f'<h2 class="woocommerce-loop-product__title">Item {number}</h2>'
        f'<img src="/item-{number}.jpg"><span class="price">{price}</span>'
        "</a></li>"
    )


def _page(
    number: int,
    total: int = 7,
    *,
    count: int | None = None,
    reported_total: int | None = None,
) -> str:
    count = count if count is not None else (24 if number < total else 9)
    first = (number - 1) * 24 + 1
    cards = "".join(_card(n) for n in range(first, first + count))
    marker = (
        '<p class="woocommerce-result-count">'
        f"Showing {first}–{first + count - 1} of {reported_total} results</p>"
        if reported_total is not None
        else ""
    )
    return (
        f'{marker}<ul class="products">{cards}</ul>'
        '<nav class="woocommerce-pagination">'
        f'<span class="page-numbers current">{number}</span>'
        f'<a class="page-numbers" href="/shop/page/{total}/?orderby=date">{total}</a>'
        "</nav>"
    )


@pytest.mark.parametrize("reported_total", [None, 153])
def test_complete_seven_page_index_needs_only_seven_requests(
    monkeypatch: pytest.MonkeyPatch,
    reported_total: int | None,
) -> None:
    responses = {
        CCCenterCatalogClient._page_url(n): _page(n, reported_total=reported_total)
        for n in range(1, 8)
    }
    requests: list[str] = []

    def fetch(url: str, **kwargs: object) -> str:
        del kwargs
        requests.append(url)
        return responses[url]

    monkeypatch.setattr(CCCenterCatalogClient, "_fetch_html", staticmethod(fetch))
    products = CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)

    assert len(products) == 153
    assert len({p.product_id for p in products}) == 153
    assert requests == list(responses)
    assert all(p.current_price == Decimal(1250) for p in products)
    assert all(p.price_status == "scalar" and p.published_at is None for p in products)


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/shop/page/2/?orderby=date",
        "https://user@cccenter.mk/shop/page/2/?orderby=date",
        "https://cccenter.mk:443/shop/page/2/?orderby=date",
        "/shop/page/0/?orderby=date",
        "/shop/page/02/?orderby=date",
        "/shop/page/2/../3/?orderby=date",
        "/shop/page/2/?orderby=price",
        "/shop/page/2/?orderby=date&orderby=price",
        "/shop/page/2/?orderby=date&product-page=3",
        "/shop/page/2/?orderby=date#fragment",
        "/shop/page/2/",
        "/other/?orderby=date",
        "",
    ],
)
def test_pagination_cannot_escape_or_change_scope(link: str) -> None:
    soup = BeautifulSoup(f'<a class="page-numbers" href="{link}">2</a>', "html.parser")
    with pytest.raises(FeedFetchError, match="MalformedPagination"):
        CCCenterCatalogClient._page_count(soup)


def test_unrecognized_pagination_does_not_silently_become_one_page() -> None:
    soup = BeautifulSoup(
        '<nav class="woocommerce-pagination"><button>Load more</button></nav>',
        "html.parser",
    )
    with pytest.raises(FeedFetchError, match="MalformedPagination"):
        CCCenterCatalogClient._page_count(soup)


def test_page_cap_has_headroom_but_still_rejects_oversized_catalogs() -> None:
    assert MAX_CCCENTER_PAGES >= 7
    soup = BeautifulSoup(_page(1, MAX_CCCENTER_PAGES + 1), "html.parser")
    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        CCCenterCatalogClient._page_count(soup)


@pytest.mark.parametrize(
    "failure",
    ["drift", "current", "duplicate", "empty", "product_limit"],
)
def test_partial_or_inconsistent_catalog_is_never_returned(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    responses = {CCCenterCatalogClient._page_url(n): _page(n) for n in range(1, 8)}
    second = CCCenterCatalogClient._page_url(2)
    expected = "PaginationDrift"
    if failure == "drift":
        responses[second] = _page(2, 8)
    elif failure == "current":
        responses[second] = _page(1)
    elif failure == "duplicate":
        responses[second] = _page(2).replace("item-25", "item-1")
        expected = "DuplicateProduct"
    elif failure == "empty":
        responses[second] = _page(2, count=0)
        expected = "EmptyPage"
    else:
        monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_PRODUCTS", 1)
        expected = "ProductLimitExceeded"
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: responses[url]),
    )
    with pytest.raises(FeedFetchError, match=expected):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


@pytest.mark.parametrize(
    ("page", "count", "reported_total", "error"),
    [
        (1, 23, None, "IncompletePage"),
        (2, 23, None, "IncompletePage"),
        (1, 23, 153, "IncompletePage"),
        (2, 23, 153, "IncompletePage"),
        (7, 25, None, "IncompletePage"),
        (7, 25, 153, "MalformedResultCount"),
        (7, 8, 153, "ResultCountMismatch"),
        (7, 0, None, "EmptyPage"),
    ],
)
def test_truncated_or_oversized_page_never_returns_a_partial_catalog(
    monkeypatch: pytest.MonkeyPatch,
    page: int,
    count: int,
    reported_total: int | None,
    error: str,
) -> None:
    responses = {
        CCCenterCatalogClient._page_url(n): _page(n, reported_total=reported_total)
        for n in range(1, 8)
    }
    responses[CCCenterCatalogClient._page_url(page)] = _page(
        page,
        count=count,
        reported_total=reported_total,
    )
    requests: list[str] = []

    def fetch(url: str, **kwargs: object) -> str:
        del kwargs
        requests.append(url)
        return responses[url]

    monkeypatch.setattr(CCCenterCatalogClient, "_fetch_html", staticmethod(fetch))
    with pytest.raises(FeedFetchError, match=error):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)
    assert len(requests) == page


@pytest.mark.parametrize(
    ("changed_page", "error"),
    [
        (_page(2, reported_total=154), "CatalogMetadataDrift"),
        (_page(2), "CatalogMetadataDrift"),
        (
            _page(2, reported_total=153).replace("Showing 25–48", "Showing 24–47"),
            "ResultCountMismatch",
        ),
        (
            _page(2, reported_total=153).replace("Showing 25–48", "Showing 25–47"),
            "ResultCountMismatch",
        ),
        (
            _page(2, reported_total=153).replace(
                "Showing 25–48 of 153 results",
                "Unknown count",
            ),
            "MalformedResultCount",
        ),
        (
            _page(2, reported_total=153)
            + '<p class="woocommerce-result-count">Showing 25–48 of 154 results</p>',
            "ConflictingResultCount",
        ),
    ],
)
def test_result_count_metadata_must_remain_coherent(
    monkeypatch: pytest.MonkeyPatch,
    changed_page: str,
    error: str,
) -> None:
    responses = {
        CCCenterCatalogClient._page_url(n): _page(n, reported_total=153)
        for n in range(1, 8)
    }
    responses[CCCenterCatalogClient._page_url(2)] = changed_page
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: responses[url]),
    )
    with pytest.raises(FeedFetchError, match=error):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


@pytest.mark.parametrize("count", [1, 24])
@pytest.mark.parametrize("with_markers", [False, True])
def test_terminal_cardinality_boundaries_are_valid(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    with_markers: bool,
) -> None:
    total = 24 + count if with_markers else None
    responses = {
        CCCENTER_FEED_URL: _page(1, 2, reported_total=total),
        CCCenterCatalogClient._page_url(2): _page(
            2,
            2,
            count=count,
            reported_total=total,
        ),
    }
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: responses[url]),
    )
    assert len(CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)) == 24 + count


def test_reported_total_must_agree_with_advertised_page_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: _page(1, reported_total=169)),
    )
    with pytest.raises(FeedFetchError, match="ResultCountMismatch"):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


def test_shutdown_during_index_parse_discards_partial_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = False

    def fetch(url: str, **kwargs: object) -> str:
        nonlocal shutdown
        del url, kwargs
        shutdown = True
        return _page(1)

    monkeypatch.setattr(CCCenterCatalogClient, "_fetch_html", staticmethod(fetch))
    with pytest.raises(FeedFetchInterruptedError):
        CCCenterCatalogClient().fetch_catalog(
            CCCENTER_FEED_URL,
            is_shutdown_requested=lambda: shutdown,
        )


def test_index_retains_unavailable_price_classes_and_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = (
        _card(1, classes="product-type-variable")
        + _card(2, price="1.250,00 ден – 2.500,00 ден")
        + _card(3, price="Call us")
        + _card(4, classes="outofstock")
    )
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: html),
    )
    products = CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)
    assert [p.price_status for p in products] == [
        "variable",
        "range",
        "unpriced",
        "scalar",
    ]
    assert products[3].is_in_stock is False


def test_detail_ignores_sticky_cart_and_unrelated_product_prices() -> None:
    listing = parse_product_listing(
        BeautifulSoup(_card(1), "html.parser").select_one("li"),
    )
    html = """
        <div class="product product-type-variable"><p class="price">99.999 ден</p></div>
        <div class="etheme-sticky-cart"><form class="variations_form">
          <p class="price">9.999 ден</p>
        </form></div>
        <main class="product product-type-simple">
          <h1 class="product_title">Item 1</h1>
          <div class="summary"><p class="price">1.250,00 ден</p></div>
          <div class="related"><div class="product-type-variable">
            <p class="price">8.888 ден</p><span class="sku">WRONG</span>
            <form class="variations_form"><select name="attribute_size"></select></form>
          </div></div>
        </main>
    """
    document = BeautifulSoup(html, "html.parser")
    before = str(document)
    product = parse_product_detail(document, listing)
    assert product.price_status == "scalar"
    assert product.current_price == Decimal(1250)
    assert product.sku == ""
    assert str(document) == before


@pytest.mark.parametrize(
    ("wrapper", "form"),
    [
        ("product product-type-variable", ""),
        (
            "product product-type-simple",
            '<form class="variations_form"><select name="attribute_size"></select></form>',
        ),
    ],
)
def test_detail_preserves_real_main_product_variations(wrapper: str, form: str) -> None:
    listing = parse_product_listing(
        BeautifulSoup(_card(1), "html.parser").select_one("li"),
    )
    html = (
        f'<main class="{wrapper}"><h1 class="product_title">Item 1</h1>'
        '<div class="summary"><p class="price">1.250 ден</p>'
        f"{form}"
        "</div></main>"
    )
    assert (
        parse_product_detail(BeautifulSoup(html, "html.parser"), listing).price_status
        == "variable"
    )


def test_ambiguous_detail_price_is_not_selected_by_document_order() -> None:
    listing = parse_product_listing(
        BeautifulSoup(_card(1), "html.parser").select_one("li"),
    )
    html = '<h1 class="product_title">Item 1</h1><p class="price">10 ден</p><p class="price">20 ден</p>'
    with pytest.raises(FeedFetchError, match="AmbiguousPrice"):
        parse_product_detail(BeautifulSoup(html, "html.parser"), listing)


def test_detail_enrichment_is_explicit_and_single_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = parse_product_listing(
        BeautifulSoup(_card(1), "html.parser").select_one("li"),
    )
    requests: list[str] = []

    def fetch(url: str, **kwargs: object) -> str:
        del kwargs
        requests.append(url)
        if url == CCCENTER_FEED_URL:
            return _card(1)
        return '<h1 class="product_title">Item 1</h1><p class="price">1.250 ден</p>'

    monkeypatch.setattr(CCCenterCatalogClient, "_fetch_html", staticmethod(fetch))
    product = CCCenterCatalogClient().fetch_product_detail(listing)
    assert requests == [listing.url]
    assert product.current_price == Decimal(1250)
    assert product.published_at is None
    client = CCCenterCatalogClient()
    index_product = client.fetch_catalog(CCCENTER_FEED_URL)[0]
    assert client.fetch_product_detail(index_product) == product
    assert requests == [listing.url, CCCENTER_FEED_URL, listing.url]


def test_unknown_nested_detail_layout_cannot_borrow_a_sidebar_price() -> None:
    listing = parse_product_listing(
        BeautifulSoup(_card(1), "html.parser").select_one("li"),
    )
    html = (
        '<div class="unknown"><h1 class="product_title">Item 1</h1></div>'
        '<div class="sidebar"><p class="price">9.999 ден</p></div>'
    )
    with pytest.raises(FeedFetchError, match="MalformedProduct"):
        parse_product_detail(BeautifulSoup(html, "html.parser"), listing)


def test_empty_variation_form_without_attributes_is_not_variation_evidence() -> None:
    listing = parse_product_listing(
        BeautifulSoup(_card(1), "html.parser").select_one("li"),
    )
    html = (
        '<main class="product"><h1 class="product_title">Item 1</h1>'
        '<p class="price">1.250 ден</p><form class="variations_form"></form></main>'
    )
    assert (
        parse_product_detail(BeautifulSoup(html, "html.parser"), listing).price_status
        == "scalar"
    )


def test_catalog_deadline_is_enforced_during_index_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_SCAN_SECONDS", 0)
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: _page(1)),
    )
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


def test_flat_detail_fragment_does_not_borrow_nested_sidebar_price() -> None:
    listing = parse_product_listing(
        BeautifulSoup(_card(1), "html.parser").select_one("li"),
    )
    html = (
        '<h1 class="product_title">Item 1</h1>'
        '<div class="sidebar"><p class="price">9.999 ден</p></div>'
    )
    product = parse_product_detail(BeautifulSoup(html, "html.parser"), listing)
    assert product.price_status == "unpriced"
    assert product.current_price != Decimal(9999)


def test_multipage_catalog_requires_current_page_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _page(1).replace('<span class="page-numbers current">1</span>', "")
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: html),
    )
    with pytest.raises(FeedFetchError, match="PaginationDrift"):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


def test_multipage_catalog_rejects_duplicate_matching_current_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _page(1) + '<span class="page-numbers current">1</span>'
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: html),
    )
    with pytest.raises(FeedFetchError, match="PaginationDrift"):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


def test_full_single_page_without_total_cannot_prove_catalog_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = "".join(_card(n) for n in range(24))
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: html),
    )
    with pytest.raises(FeedFetchError, match="AmbiguousCatalogEnd"):
        CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)


def test_full_single_page_with_coherent_total_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = "".join(_card(n) for n in range(24))
    html += '<p class="woocommerce-result-count">Showing all 24 results</p>'
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: html),
    )
    assert len(CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)) == 24


def test_short_single_page_without_total_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = "".join(_card(n) for n in range(23))
    monkeypatch.setattr(
        CCCenterCatalogClient,
        "_fetch_html",
        staticmethod(lambda url, **kwargs: html),
    )
    assert len(CCCenterCatalogClient().fetch_catalog(CCCENTER_FEED_URL)) == 23
