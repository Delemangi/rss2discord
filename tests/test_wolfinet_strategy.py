"""Strategy, URL scope, catalog bounds and HTTP mapping for Wolfinet."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryId, SourceMetric
from rss2discord.providers.wolfinet import catalog as wolfinet_catalog
from rss2discord.providers.wolfinet.bounds import (
    MAX_WOLFINET_LATEST_PAGES,
    MAX_WOLFINET_PAGES,
    MAX_WOLFINET_RESPONSE_BYTES,
    WOLFINET_INTER_REQUEST_DELAY,
    WOLFINET_LABEL,
    WOLFINET_PRODUCTS_PER_PAGE,
)
from rss2discord.providers.wolfinet.catalog import (
    WolfinetCatalogClient,
    page_url,
    validate_wolfinet_url,
)
from rss2discord.providers.wolfinet.models import WolfinetProduct
from rss2discord.providers.wolfinet.strategy import WolfinetStrategy
from rss2discord.retries import FeedFetchInterruptedError

BASE_URL = "https://wolfinet.com.mk/en/shop"
OBSERVED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def product(
    product_id: str,
    *,
    amount: str = "100.00",
    name: str | None = None,
    description: str = "",
) -> WolfinetProduct:
    return WolfinetProduct(
        product_id=product_id,
        name=name or f"Product {product_id}",
        url=f"https://wolfinet.com.mk/en/shop/product-{product_id}",
        image_url=f"https://wolfinet.com.mk/web/image/product.template/{product_id}/image",
        description=description,
        amount=Decimal(amount),
        formatted_price=f"{amount} ден",
        currency="MKD",
        observed_at=OBSERVED_AT,
    )


@pytest.mark.parametrize(
    "url",
    [
        BASE_URL,
        f"{BASE_URL}/",
    ],
)
def test_wolfinet_url_scope_accepts_the_shop_root(url: str) -> None:
    assert validate_wolfinet_url(url) == BASE_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://wolfinet.com.mk/en/shop",
        "https://user:password@wolfinet.com.mk/en/shop",
        "https://wolfinet.com.mk:443/en/shop",
        f"{BASE_URL}#products",
        f"{BASE_URL}?order=create_date+desc",
        "https://wolfinet.com.mk/en/shop/ubiquiti-unifi-6-lite-4101",
        "https://wolfinet.com.mk/en/shop/page/2",
        "https://wolfinet.com.mk/en/",
        "https://wolfinet.com.mk/",
        "https://www.wolfinet.com.mk/en/shop",
        "https://example.com/en/shop",
        "https://wolfinet.com.mk/en/shop/extra",
    ],
)
def test_wolfinet_url_scope_rejects_non_shop_roots(url: str) -> None:
    with pytest.raises(FeedFetchError) as error:
        validate_wolfinet_url(url)

    assert error.value.cause_type == "InvalidUrl"


def test_wolfinet_page_url_builds_newest_first_pagination() -> None:
    assert page_url(BASE_URL, 1) == f"{BASE_URL}?order=create_date+desc"
    assert page_url(BASE_URL, 3) == f"{BASE_URL}/page/3?order=create_date+desc"


def test_wolfinet_page_size_constant() -> None:
    assert WOLFINET_PRODUCTS_PER_PAGE == 20


def test_wolfinet_strategy_preserves_seed_contract() -> None:
    strategy = WolfinetStrategy()

    assert strategy.seed_existing_on_first_fetch is True
    assert strategy.require_entries_for_initialization is True


def test_wolfinet_strategy_maps_newest_first_page_to_oldest_first_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest_first = (product("3"), product("2"), product("1"))

    def fetch_latest(
        self: WolfinetCatalogClient,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[WolfinetProduct, ...]:
        del self, url, is_shutdown_requested
        return newest_first

    monkeypatch.setattr(WolfinetCatalogClient, "fetch_latest", fetch_latest)

    entries, source = WolfinetStrategy().fetch_entries(BASE_URL)

    assert [entry.product_id for entry in entries] == ["1", "2", "3"]
    assert source == WOLFINET_LABEL


def test_wolfinet_strategy_maps_entry_data_and_numeric_id() -> None:
    entry = product(
        "4101",
        name="Ubiquiti UniFi 6 Lite",
        amount="2364.41",
        description="Wi-Fi 6 access point &amp; PoE powered",
    )

    data = WolfinetStrategy().get_entry_data(entry)

    assert WolfinetStrategy().get_entry_id(entry) == EntryId("4101")
    assert data.title == "Ubiquiti UniFi 6 Lite"
    assert data.link == entry.url
    assert data.image_url == entry.image_url
    assert data.description == "Wi-Fi 6 access point & PoE powered"
    assert data.timestamp == OBSERVED_AT.isoformat()
    assert data.categories == ()
    assert data.source_metrics == (SourceMetric("Price", "2364.41 ден"),)


def test_wolfinet_strategy_truncates_long_descriptions() -> None:
    entry = product("1", description="x" * 2_500)

    data = WolfinetStrategy().get_entry_data(entry)

    assert len(data.description) == 2_000
    assert data.description.endswith("...")


def test_wolfinet_catalog_dedupes_ids_and_stops_on_an_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        page_url(BASE_URL, 1): (product("1"), product("2")),
        page_url(BASE_URL, 2): (product("2"), product("3")),
        page_url(BASE_URL, 3): (),
    }
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[tuple[WolfinetProduct, ...], int]:
        del remaining_bytes
        calls.append(url)
        return pages[url], 1

    monkeypatch.setattr(WolfinetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    products = WolfinetCatalogClient(sleep=lambda _seconds: None).fetch_catalog(
        BASE_URL,
    )

    assert [item.product_id for item in products] == ["1", "2", "3"]
    assert calls == [
        page_url(BASE_URL, 1),
        page_url(BASE_URL, 2),
        page_url(BASE_URL, 3),
    ]


def _page_number(url: str) -> int:
    path = url.split("?", 1)[0]
    return int(path.rsplit("/page/", 1)[1]) if "/page/" in path else 1


def test_wolfinet_catalog_stops_at_the_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[tuple[WolfinetProduct, ...], int]:
        del remaining_bytes
        calls.append(url)
        return (product(str(_page_number(url))),), 1

    monkeypatch.setattr(WolfinetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    products = WolfinetCatalogClient(sleep=lambda _seconds: None).fetch_catalog(
        BASE_URL,
    )

    assert len(calls) == MAX_WOLFINET_PAGES
    assert [item.product_id for item in products] == [
        str(page_number) for page_number in range(1, MAX_WOLFINET_PAGES + 1)
    ]


def test_wolfinet_latest_reads_two_pages_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        page_url(BASE_URL, 1): (product("3"), product("2")),
        page_url(BASE_URL, 2): (product("2"), product("1")),
    }
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[tuple[WolfinetProduct, ...], int]:
        del remaining_bytes
        calls.append(url)
        return pages[url], 1

    monkeypatch.setattr(WolfinetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    products = WolfinetCatalogClient(sleep=lambda _seconds: None).fetch_latest(BASE_URL)

    assert [item.product_id for item in products] == ["3", "2", "1"]
    assert calls == [page_url(BASE_URL, 1), page_url(BASE_URL, 2)]
    assert len(calls) == MAX_WOLFINET_LATEST_PAGES


def test_wolfinet_latest_stops_when_the_first_page_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[tuple[WolfinetProduct, ...], int]:
        del remaining_bytes
        calls.append(url)
        return (), 1

    monkeypatch.setattr(WolfinetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    products = WolfinetCatalogClient(sleep=lambda _seconds: None).fetch_latest(BASE_URL)

    assert products == ()
    assert calls == [page_url(BASE_URL, 1)]


def test_wolfinet_catalog_uses_the_polite_inter_page_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[tuple[WolfinetProduct, ...], int]:
        del remaining_bytes
        return (product(str(_page_number(url))),), 1

    monkeypatch.setattr(WolfinetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    WolfinetCatalogClient(sleep=sleeps.append).fetch_latest(BASE_URL)

    assert sleeps == [WOLFINET_INTER_REQUEST_DELAY]


def test_wolfinet_catalog_observes_shutdown_before_fetching() -> None:
    client = WolfinetCatalogClient(sleep=lambda _seconds: None)
    with pytest.raises(FeedFetchInterruptedError):
        client.fetch_catalog(
            BASE_URL,
            is_shutdown_requested=lambda: True,
        )


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        content_length: int | None = None,
        body: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )
        self.url = BASE_URL
        self.encoding = "utf-8"
        self._body = body

    def iter_content(self, chunk_size: int) -> list[bytes]:
        del chunk_size
        return [self._body]

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(404, False), (429, True), (503, True)],
)
def test_wolfinet_http_errors_map_to_feed_fetch_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    retryable: bool,
) -> None:
    monkeypatch.setattr(
        wolfinet_catalog.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(status_code),
    )

    with pytest.raises(FeedFetchError) as error:
        WolfinetCatalogClient._fetch_html(
            page_url(BASE_URL, 1),
            remaining_bytes=MAX_WOLFINET_RESPONSE_BYTES,
        )

    assert error.value.cause_type == "HTTPError"
    assert error.value.status_code == status_code
    assert error.value.retryable is retryable


def test_wolfinet_timeouts_map_to_retryable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> FakeResponse:
        raise requests.Timeout("slow")

    monkeypatch.setattr(wolfinet_catalog.requests, "get", timeout)

    with pytest.raises(FeedFetchError) as error:
        WolfinetCatalogClient._fetch_html(
            page_url(BASE_URL, 1),
            remaining_bytes=MAX_WOLFINET_RESPONSE_BYTES,
        )

    assert error.value.retryable is True


def test_wolfinet_oversized_responses_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wolfinet_catalog.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            content_length=MAX_WOLFINET_RESPONSE_BYTES + 1,
        ),
    )

    with pytest.raises(FeedFetchError) as error:
        WolfinetCatalogClient._fetch_html(
            page_url(BASE_URL, 1),
            remaining_bytes=MAX_WOLFINET_RESPONSE_BYTES,
        )

    assert error.value.cause_type == "ResponseTooLarge"
