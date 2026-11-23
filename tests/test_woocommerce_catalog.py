from urllib.parse import parse_qs, urlsplit

import pytest

from rss2discord.providers.woocommerce import http as woocommerce_http
from rss2discord.providers.woocommerce.catalog import WooCommerceCatalogClient
from rss2discord.providers.woocommerce.models import WooCommerceProduct
from rss2discord.transports import FeedFetchError
from tests.woocommerce_helpers import (
    CATEGORY_URL,
    SHOP_URL,
    RecordingGet,
    StubResponse,
    categories_payload,
    category_payload,
    no_wait_fetch_retry_policy,
    product_payload,
    products_payload,
)


def test_catalog_enforces_the_wall_clock_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter((0.0, 301.0))
    monkeypatch.setattr(
        "rss2discord.providers.woocommerce.catalog.time.monotonic",
        lambda: next(times),
    )

    with pytest.raises(FeedFetchError) as error:
        WooCommerceCatalogClient().fetch_catalog(
            SHOP_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert error.value.cause_type == "ScanTimeLimitExceeded"


def catalog(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[StubResponse],
) -> tuple[tuple[WooCommerceProduct, ...], RecordingGet]:
    get = RecordingGet(responses)
    monkeypatch.setattr(woocommerce_http.requests, "get", get)
    products = WooCommerceCatalogClient().fetch_catalog(
        SHOP_URL,
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=lambda: False,
    )
    return products, get


def _page(
    product_ids: list[int],
    *,
    total: int,
    pages: int,
) -> StubResponse:
    return StubResponse(
        products_payload([product_payload(product_id) for product_id in product_ids]),
        headers={"X-WP-Total": str(total), "X-WP-TotalPages": str(pages)},
    )


def test_woocommerce_catalog_paginates_until_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    products, get = catalog(
        monkeypatch,
        [
            _page(list(range(1, 101)), total=101, pages=2),
            _page([101], total=101, pages=2),
        ],
    )

    assert [product.id for product in products] == list(range(1, 102))
    queries = [parse_qs(urlsplit(url).query) for url in get.urls]
    assert [query["page"] for query in queries] == [["1"], ["2"]]
    assert all(query["per_page"] == ["100"] for query in queries)


def test_woocommerce_catalog_accepts_empty_shop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    products, _get = catalog(
        monkeypatch,
        [
            StubResponse(
                products_payload([]),
                headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"},
            ),
        ],
    )

    assert products == ()


def test_woocommerce_catalog_rejects_incomplete_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _page([1], total=2, pages=1)
    with pytest.raises(FeedFetchError, match="IncompleteCatalog"):
        catalog(monkeypatch, [response, response, response])


def test_woocommerce_catalog_rejects_duplicate_product_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        _page(list(range(1, 101)), total=101, pages=2),
        _page([1], total=101, pages=2),
    ]
    with pytest.raises(FeedFetchError, match="DuplicateProductId"):
        catalog(monkeypatch, pages)


def test_woocommerce_catalog_rejects_pagination_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _page(list(range(1, 101)), total=101, pages=2)
    second = _page([101], total=102, pages=2)
    with pytest.raises(FeedFetchError, match="PaginationDrift"):
        catalog(monkeypatch, [first, second] * 3)


def test_woocommerce_catalog_rejects_product_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = StubResponse(
        products_payload([]),
        headers={"X-WP-Total": "5001", "X-WP-TotalPages": "51"},
    )
    with pytest.raises(FeedFetchError, match="ProductLimitExceeded"):
        catalog(monkeypatch, [response])


def test_woocommerce_catalog_rejects_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = StubResponse(
        products_payload([]),
        headers={"X-WP-Total": "100", "X-WP-TotalPages": "51"},
    )
    with pytest.raises(FeedFetchError, match="PageLimitExceeded"):
        catalog(monkeypatch, [response])


def test_woocommerce_catalog_rejects_unknown_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                categories_payload(
                    [category_payload(100, "SSD/HDD", "ssd-hdd")],
                ),
            ),
        ],
    )
    monkeypatch.setattr(woocommerce_http.requests, "get", get)

    with pytest.raises(FeedFetchError, match="UnknownCategory"):
        WooCommerceCatalogClient().fetch_catalog(
            CATEGORY_URL.replace("ssd-hdd", "missing"),
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: False,
        )
