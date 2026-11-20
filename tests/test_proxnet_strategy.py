"""Strategy, URL scope, catalog bounds and HTTP mapping for PROXNet."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryId, SourceMetric
from rss2discord.providers.proxnet import catalog as proxnet_catalog
from rss2discord.providers.proxnet.bounds import (
    MAX_PROXNET_LATEST_PAGES,
    MAX_PROXNET_PAGES,
    MAX_PROXNET_RESPONSE_BYTES,
    PROXNET_INTER_REQUEST_DELAY,
    PROXNET_PRODUCTS_PER_PAGE,
)
from rss2discord.providers.proxnet.catalog import (
    ProxnetCatalogClient,
    page_url,
    validate_proxnet_url,
)
from rss2discord.providers.proxnet.models import ProxnetPage, ProxnetProduct
from rss2discord.providers.proxnet.strategy import ProxnetStrategy
from rss2discord.retries import FeedFetchInterruptedError

FIXTURES = Path(__file__).parent / "fixtures" / "proxnet"
BASE_URL = "https://www.proxnet.mk/mk/17-mrezhna-oprema"
OBSERVED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def product(
    product_id: str,
    *,
    amount: str = "100.00",
    name: str | None = None,
    description: str = "",
    category: str = "Мрежна опрема",
) -> ProxnetProduct:
    return ProxnetProduct(
        product_id=product_id,
        name=name or f"Product {product_id}",
        url=f"https://www.proxnet.mk/mk/{product_id}-product-{product_id}.html",
        image_url=f"https://www.proxnet.mk/{product_id}.jpg",
        description=description,
        amount=Decimal(amount),
        formatted_price=f"{amount} ден",
        currency="MKD",
        observed_at=OBSERVED_AT,
        category=category,
    )


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "url",
    [
        BASE_URL,
        "https://www.proxnet.mk/mk/17-mrezhna-oprema/",
        "https://proxnet.mk/mk/17-mrezhna-oprema",
        f"{BASE_URL}?orderby=price&n=12",
    ],
)
def test_proxnet_url_scope_accepts_category_pages(url: str) -> None:
    assert validate_proxnet_url(url) == BASE_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://www.proxnet.mk/mk/17-mrezhna-oprema",
        "https://user:password@www.proxnet.mk/mk/17-mrezhna-oprema",
        "https://www.proxnet.mk:443/mk/17-mrezhna-oprema",
        f"{BASE_URL}#products",
        "https://www.proxnet.mk/mk/1152-heat-shrink-sleeves-40-mm.html",
        "https://www.proxnet.mk/mk/search",
        "https://www.proxnet.mk/mk/cart",
        "https://www.proxnet.mk/mk/account",
        "https://www.proxnet.mk/mk/17-mrezhna-oprema/extra",
        "https://www.proxnet.mk/mk/",
        "https://www.proxnet.mk/",
        "https://www.proxnet.mk/proizvod/1152-product.html",
        "https://example.com/mk/17-mrezhna-oprema",
        "https://www.proxnet.mk/mk/17_mrezhna_oprema",
    ],
)
def test_proxnet_url_scope_rejects_non_category_urls(url: str) -> None:
    with pytest.raises(FeedFetchError) as error:
        validate_proxnet_url(url)

    assert error.value.cause_type == "InvalidUrl"


def test_proxnet_page_url_builds_the_provider_owned_query() -> None:
    assert page_url(BASE_URL, 3) == (
        f"{BASE_URL}?orderby=date_add&orderway=desc&n={PROXNET_PRODUCTS_PER_PAGE}&p=3"
    )


def test_proxnet_strategy_preserves_seed_contract() -> None:
    strategy = ProxnetStrategy()

    assert strategy.seed_existing_on_first_fetch is True
    assert strategy.require_entries_for_initialization is True


def test_proxnet_strategy_maps_newest_first_page_to_oldest_first_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ProxnetPage(
        products=(product("3"), product("2"), product("1")),
        title="Мрежна опрема",
    )

    def fetch_latest(
        self: ProxnetCatalogClient,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> ProxnetPage:
        del self, url, is_shutdown_requested
        return page

    monkeypatch.setattr(ProxnetCatalogClient, "fetch_latest", fetch_latest)

    entries, source = ProxnetStrategy().fetch_entries(BASE_URL)

    assert [entry.product_id for entry in entries] == ["1", "2", "3"]
    assert source == "Мрежна опрема"


def test_proxnet_strategy_maps_entry_data_and_numeric_id() -> None:
    entry = product(
        "1152",
        name="Heat Shrink Sleeves 40 mm",
        amount="9.00",
        description="Термо смалувачки ракав &amp; заштита",
    )

    data = ProxnetStrategy().get_entry_data(entry)

    assert ProxnetStrategy().get_entry_id(entry) == EntryId("1152")
    assert data.title == "Heat Shrink Sleeves 40 mm"
    assert data.link == entry.url
    assert data.image_url == entry.image_url
    assert data.description == "Термо смалувачки ракав & заштита"
    assert data.timestamp == OBSERVED_AT.isoformat()
    assert data.categories == ("Мрежна опрема",)
    assert data.source_metrics == (SourceMetric("Price", "9.00 ден"),)


def test_proxnet_strategy_truncates_long_descriptions() -> None:
    entry = product("1", description="x" * 2_500)

    data = ProxnetStrategy().get_entry_data(entry)

    assert len(data.description) == 2_000
    assert data.description.endswith("...")


def test_proxnet_catalog_dedupes_ids_and_stops_on_an_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        page_url(BASE_URL, 1): ProxnetPage((product("1"), product("2")), "Category"),
        page_url(BASE_URL, 2): ProxnetPage((product("2"), product("3")), "Category"),
        page_url(BASE_URL, 3): ProxnetPage((), "Category"),
    }
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[ProxnetPage, int]:
        del remaining_bytes
        calls.append(url)
        return pages[url], 1

    monkeypatch.setattr(ProxnetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    products = ProxnetCatalogClient(sleep=lambda _seconds: None).fetch_catalog(BASE_URL)

    assert [item.product_id for item in products] == ["1", "2", "3"]
    assert calls == [
        page_url(BASE_URL, 1),
        page_url(BASE_URL, 2),
        page_url(BASE_URL, 3),
    ]


def test_proxnet_catalog_stops_at_the_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[ProxnetPage, int]:
        del remaining_bytes
        calls.append(url)
        page_number = int(url.rsplit("p=", 1)[1])
        return ProxnetPage((product(str(page_number)),), "Category"), 1

    monkeypatch.setattr(ProxnetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    products = ProxnetCatalogClient(sleep=lambda _seconds: None).fetch_catalog(BASE_URL)

    assert len(calls) == MAX_PROXNET_PAGES
    assert [item.product_id for item in products] == [
        str(page_number) for page_number in range(1, MAX_PROXNET_PAGES + 1)
    ]


def test_proxnet_latest_reads_two_pages_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        page_url(BASE_URL, 1): ProxnetPage((product("1"), product("2")), "Category"),
        page_url(BASE_URL, 2): ProxnetPage((product("2"), product("3")), "Category"),
    }
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[ProxnetPage, int]:
        del remaining_bytes
        calls.append(url)
        return pages[url], 1

    monkeypatch.setattr(ProxnetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    page = ProxnetCatalogClient(sleep=lambda _seconds: None).fetch_latest(BASE_URL)

    assert [item.product_id for item in page.products] == ["1", "2", "3"]
    assert page.title == "Category"
    assert calls == [page_url(BASE_URL, 1), page_url(BASE_URL, 2)]
    assert len(calls) == MAX_PROXNET_LATEST_PAGES


def test_proxnet_latest_stops_when_the_first_page_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[ProxnetPage, int]:
        del remaining_bytes
        calls.append(url)
        return ProxnetPage((), "Category"), 1

    monkeypatch.setattr(ProxnetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    page = ProxnetCatalogClient(sleep=lambda _seconds: None).fetch_latest(BASE_URL)

    assert page.products == ()
    assert page.title == "Category"
    assert calls == [page_url(BASE_URL, 1)]


def test_proxnet_catalog_uses_the_polite_inter_page_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    def fetch_page(
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[ProxnetPage, int]:
        del remaining_bytes
        page_number = int(url.rsplit("p=", 1)[1])
        return ProxnetPage((product(str(page_number)),), "Category"), 1

    monkeypatch.setattr(ProxnetCatalogClient, "_fetch_page", staticmethod(fetch_page))

    ProxnetCatalogClient(sleep=sleeps.append).fetch_latest(BASE_URL)

    assert sleeps == [PROXNET_INTER_REQUEST_DELAY]


def test_proxnet_catalog_observes_shutdown_before_fetching() -> None:
    with pytest.raises(FeedFetchInterruptedError):
        ProxnetCatalogClient(sleep=lambda _seconds: None).fetch_catalog(
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
def test_proxnet_http_errors_map_to_feed_fetch_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    retryable: bool,
) -> None:
    monkeypatch.setattr(
        proxnet_catalog.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(status_code),
    )

    with pytest.raises(FeedFetchError) as error:
        ProxnetCatalogClient._fetch_html(
            page_url(BASE_URL, 1),
            remaining_bytes=MAX_PROXNET_RESPONSE_BYTES,
        )

    assert error.value.cause_type == "HTTPError"
    assert error.value.status_code == status_code
    assert error.value.retryable is retryable


def test_proxnet_timeouts_map_to_retryable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> FakeResponse:
        raise requests.Timeout("slow")

    monkeypatch.setattr(proxnet_catalog.requests, "get", timeout)

    with pytest.raises(FeedFetchError) as error:
        ProxnetCatalogClient._fetch_html(
            page_url(BASE_URL, 1),
            remaining_bytes=MAX_PROXNET_RESPONSE_BYTES,
        )

    assert error.value.retryable is True


def test_proxnet_oversized_responses_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxnet_catalog.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            content_length=MAX_PROXNET_RESPONSE_BYTES + 1,
        ),
    )

    with pytest.raises(FeedFetchError) as error:
        ProxnetCatalogClient._fetch_html(
            page_url(BASE_URL, 1),
            remaining_bytes=MAX_PROXNET_RESPONSE_BYTES,
        )

    assert error.value.cause_type == "ResponseTooLarge"
