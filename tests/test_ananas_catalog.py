from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from curl_cffi import requests as curl_requests

from rss2discord.providers.ananas import catalog as ananas_catalog
from rss2discord.providers.ananas.bounds import (
    ANANAS_ALGOLIA_APPLICATION_ID,
    ANANAS_ALGOLIA_ENDPOINT,
    ANANAS_ALGOLIA_INDEX,
    ANANAS_ALGOLIA_SEARCH_KEY_ENV,
    ANANAS_CATEGORY_FILTERS,
    ANANAS_COMPONENTS_FEED_URL,
    ANANAS_FEED_URL,
    ANANAS_PAGE_SIZE,
)
from rss2discord.providers.ananas.catalog import (
    AnanasCatalogClient,
    validate_ananas_url,
)
from rss2discord.transports import FeedFetchError

DUMMY_SEARCH_KEY = "dummy-ananas-search-key"


@pytest.fixture(autouse=True)
def _set_ananas_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ANANAS_ALGOLIA_SEARCH_KEY_ENV, DUMMY_SEARCH_KEY)


@dataclass
class _FakeResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(
        default_factory=lambda: {"Content-Type": "application/json"},
    )
    encoding: str = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise curl_requests.exceptions.HTTPError(
                f"HTTP {self.status_code}",
            )


class FakeAlgolia:
    def __init__(self, pages: Mapping[int, dict[str, object]]) -> None:
        self.pages = dict(pages)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout: float,
        allow_redirects: bool,
        stream: bool,
        content_callback: Callable[[bytes], int],
    ) -> _FakeResponse:
        del timeout, allow_redirects, stream
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": dict(json_body),
            },
        )
        page = json_body.get("page")
        assert isinstance(page, int)
        content_callback(json.dumps(self.pages[page]).encode("utf-8"))
        return _FakeResponse()


def _hit(
    index: int,
    *,
    object_id: str | None = None,
    slug: str | None = None,
    price: object = 100,
    base_price: object = None,
    on_stock: object = True,
    available: object = 1,
    merchant: str = "Shop",
    name: str | None = None,
    first_published: object = 1_710_511_159_942,
) -> dict[str, object]:
    return {
        "objectID": object_id if object_id is not None else str(index),
        "price": price,
        "basePrice": base_price,
        "onSale": False,
        "onStock": on_stock,
        "available": available,
        "firstPublishedTimestamp": first_published,
        "merchant": {"displayName": merchant},
        "product": {
            "name": name if name is not None else f"Product {index}",
            "slug": slug if slug is not None else f"product-{index}",
            "brand": "Brand",
            "thumbnailUrl": "/tmp/image-thumbnails/item.jpg",  # noqa: S108
            "coverImageUrl": "/assets/cover.jpg",
            "categoryNames": ["IT Shop", "Computers"],
            "hasVariations": False,
            "newProduct": False,
        },
    }


def _page(
    hits: list[dict[str, object]],
    *,
    total: int | None = None,
    pages: int | None = None,
    page: int = 0,
) -> dict[str, object]:
    resolved_total = len(hits) if total is None else total
    resolved_pages = (
        (resolved_total + ANANAS_PAGE_SIZE - 1) // ANANAS_PAGE_SIZE
        if pages is None
        else pages
    )
    return {
        "hits": hits,
        "nbHits": resolved_total,
        "page": page,
        "nbPages": resolved_pages,
        "hitsPerPage": ANANAS_PAGE_SIZE,
        "index": ANANAS_ALGOLIA_INDEX,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://ananas.mk/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema",
        "https://www.ananas.mk/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema",
        "https://ananas.mk:443/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema",
        "https://ananas.mk/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema/",
        "https://ananas.mk/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema?x=1",
        "https://ananas.mk/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema#x",
    ],
)
def test_ananas_rejects_noncanonical_feed_urls(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        validate_ananas_url(url)


def test_ananas_full_scan_paginates_and_uses_supported_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page_hits = [_hit(index) for index in range(48)]
    last_page_hits = [_hit(48)]
    fake = FakeAlgolia(
        {
            0: _page(first_page_hits, total=49, pages=2),
            1: _page(last_page_hits, total=49, pages=2, page=1),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)

    assert len(products) == 49
    assert [call["body"]["page"] for call in fake.calls] == [0, 1, 0]
    assert fake.calls[0]["url"] == ANANAS_ALGOLIA_ENDPOINT
    assert ANANAS_CATEGORY_FILTERS[ANANAS_FEED_URL] == (
        'product.categories.lvl1:"IT Shop > Компјутери и компјутерска опрема"'
    )
    assert fake.calls[0]["body"]["filters"] == ANANAS_CATEGORY_FILTERS[ANANAS_FEED_URL]
    assert "numericFilters" not in fake.calls[0]["body"]
    headers = fake.calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["x-algolia-application-id"] == ANANAS_ALGOLIA_APPLICATION_ID
    assert headers["x-algolia-api-key"] == DUMMY_SEARCH_KEY


def test_ananas_components_category_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia({0: _page([_hit(1)])})
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_catalog(ANANAS_COMPONENTS_FEED_URL)

    assert len(products) == 1
    assert ANANAS_CATEGORY_FILTERS[ANANAS_COMPONENTS_FEED_URL] == (
        'product.categories.lvl1:"IT Shop > Компјутерски компоненти"'
    )
    assert fake.calls[0]["body"]["filters"] == (
        'product.categories.lvl1:"IT Shop > Компјутерски компоненти"'
    )


def test_ananas_parses_hit_fields_and_builds_product_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia(
        {
            0: _page(
                [
                    _hit(
                        1,
                        object_id="1462229",
                        slug="asus-gaming-monitor",
                        name="ASUS Gaming Монитор &amp; Stand",
                        price=7403,
                        base_price=8990,
                        merchant="AНАНАС ШОП",
                    ),
                ],
            ),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)

    assert len(products) == 1
    product = products[0]
    assert product.inventory_id == "1462229"
    assert product.product_id == "asus-gaming-monitor"
    assert product.name == "ASUS Gaming Монитор & Stand"
    assert product.url == ("https://ananas.mk/proizvod/asus-gaming-monitor/1462229")
    assert product.image_url == ("https://static.ananas.rs/assets/cover.jpg")
    assert product.brand == "Brand"
    assert product.categories == ("IT Shop", "Computers")
    assert product.price == 7403
    assert product.base_price == 8990
    assert product.currency == "MKD"
    assert product.in_stock is True
    assert product.available == 1
    assert product.merchant == "AНАНАС ШОП"
    assert product.first_published_ms == 1_710_511_159_942
    assert product.has_variations is False


def test_ananas_groups_offers_by_slug_choosing_lowest_in_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia(
        {
            0: _page(
                [
                    _hit(1, object_id="1", slug="alpha", price=1200, on_stock=True),
                    _hit(2, object_id="2", slug="alpha", price=1000, on_stock=True),
                    _hit(3, object_id="3", slug="beta", price=500, on_stock=False),
                ],
            ),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)

    assert [product.product_id for product in products] == ["alpha", "beta"]
    alpha, beta = products
    assert alpha.inventory_id == "2"
    assert alpha.price == 1000
    assert alpha.url == "https://ananas.mk/proizvod/alpha/2"
    assert beta.inventory_id == "3"
    assert beta.in_stock is False


@pytest.mark.parametrize(
    ("offers", "expected_inventory_id", "expected_price"),
    [
        (
            [
                _hit(1, object_id="1", slug="alpha", price=1200, on_stock=True),
                _hit(2, object_id="2", slug="alpha", price=1000, on_stock=False),
            ],
            "1",
            1200,
        ),
        (
            [
                _hit(1, object_id="1", slug="alpha", price=1200, on_stock=False),
                _hit(2, object_id="2", slug="alpha", price=1000, on_stock=False),
            ],
            "2",
            1000,
        ),
    ],
)
def test_ananas_prefers_in_stock_then_falls_back_to_lowest_price(
    monkeypatch: pytest.MonkeyPatch,
    offers: list[dict[str, object]],
    expected_inventory_id: str,
    expected_price: int,
) -> None:
    fake = FakeAlgolia({0: _page(offers)})
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)

    assert len(products) == 1
    assert products[0].inventory_id == expected_inventory_id
    assert products[0].price == expected_price


def test_ananas_rejects_duplicate_inventory_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia(
        {
            0: _page(
                [
                    _hit(1, object_id="7", slug="alpha"),
                    _hit(2, object_id="7", slug="beta"),
                ],
            ),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    with pytest.raises(FeedFetchError, match="IncompleteCatalog"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)


def test_ananas_tolerates_metadata_drift_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia(
        {
            0: _page([_hit(index) for index in range(48)], total=49, pages=2),
            1: _page([_hit(48)], total=50, pages=2, page=1),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)

    assert len(products) == 49


def test_ananas_rejects_inconsistent_page_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia(
        {
            0: _page([_hit(index) for index in range(48)], total=49, pages=2),
            1: _page([_hit(48)], total=49, pages=3, page=1),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)


def test_ananas_rejects_low_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia(
        {
            0: _page([_hit(0)], total=100, pages=3),
            1: _page([], total=100, pages=3, page=1),
            2: _page([], total=100, pages=3, page=2),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    with pytest.raises(FeedFetchError, match="IncompleteCatalog"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)


def test_ananas_accepts_float_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAlgolia(
        {0: _page([_hit(1, price=7403.0, base_price=8990.0)])},
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)

    assert len(products) == 1
    assert products[0].price == Decimal(7403)
    assert products[0].base_price == Decimal(8990)


@pytest.mark.parametrize("price", [0, -1, "100", True, 10**13])
def test_ananas_rejects_invalid_prices(
    monkeypatch: pytest.MonkeyPatch,
    price: object,
) -> None:
    fake = FakeAlgolia({0: _page([_hit(1, price=price)])})
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    with pytest.raises(FeedFetchError, match="MalformedPrice"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)


def test_ananas_requires_search_key_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ANANAS_ALGOLIA_SEARCH_KEY_ENV)
    calls: list[object] = []

    def perform(*args: object, **kwargs: object) -> _FakeResponse:
        del args, kwargs
        calls.append(object())
        return _FakeResponse()

    monkeypatch.setattr(ananas_catalog, "_perform_request", perform)

    with pytest.raises(FeedFetchError, match="MissingSearchKey"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)

    assert calls == []


def test_ananas_enforces_response_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ananas_catalog, "MAX_ANANAS_RESPONSE_BYTES", 3)

    def perform(
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, object],
        timeout: float,
        allow_redirects: bool,
        stream: bool,
        content_callback: Callable[[bytes], int],
    ) -> _FakeResponse:
        del url, headers, json_body, timeout, allow_redirects, stream
        content_callback(b"1234")
        return _FakeResponse()

    monkeypatch.setattr(ananas_catalog, "_perform_request", perform)

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)


def test_ananas_enforces_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAlgolia(
        {
            0: _page(
                [_hit(index) for index in range(48)],
                total=48 * 101,
                pages=101,
            ),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)


def test_ananas_enforces_product_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAlgolia(
        {
            0: _page(
                [_hit(index) for index in range(48)],
                total=10_001,
                pages=209,
            ),
        },
    )
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        AnanasCatalogClient().fetch_catalog(ANANAS_FEED_URL)


def test_ananas_latest_scan_uses_numeric_filter_and_fetches_all_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ananas_catalog,
        "_latest_cutoff_ms",
        lambda: 1_700_000_000_000,
    )
    pages = {
        page: _page(
            [
                _hit(page * ANANAS_PAGE_SIZE + offset)
                for offset in range(ANANAS_PAGE_SIZE)
            ],
            total=480,
            pages=10,
            page=page,
        )
        for page in range(10)
    }
    fake = FakeAlgolia(pages)
    monkeypatch.setattr(ananas_catalog, "_perform_request", fake)

    products = AnanasCatalogClient().fetch_latest_products(ANANAS_FEED_URL)

    assert len(products) == 10 * ANANAS_PAGE_SIZE
    assert [call["body"]["page"] for call in fake.calls] == list(range(10))
    for call in fake.calls:
        assert call["body"]["numericFilters"] == [
            "firstPublishedTimestamp > 1700000000000",
        ]
        assert call["body"]["filters"] == ANANAS_CATEGORY_FILTERS[ANANAS_FEED_URL]
