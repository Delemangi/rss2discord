from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.cccenter import catalog as cccenter_catalog
from rss2discord.providers.cccenter.catalog import CCCenterCatalogClient
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from rss2discord.retries import FeedFetchInterruptedError

DETAIL = (
    '<div class="product"><h1 class="product_title">Selected product</h1>'
    '<div class="summary"><span class="price">90,00 ден</span></div></div>'
).encode()


def listing(number: int) -> CCCenterListing:
    url = f"https://cccenter.mk/product/item-{number}/"
    return CCCenterListing(url, "Selected product", url, Decimal(90), None, None)


@dataclass
class Response:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    encoding: str = "utf-8"

    def raise_for_status(self) -> None:
        pass


@dataclass
class DetailTransport:
    now: float = 0
    durations: list[float] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    response: Response = field(default_factory=Response)

    def request(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
        stream: bool,
        content_callback: Callable[[bytes], int],
    ) -> Response:
        del headers, stream
        assert allow_redirects is False
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self.durations:
            self.now += self.durations.pop(0)
        content_callback(DETAIL)
        return self.response


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> DetailTransport:
    result = DetailTransport()
    monkeypatch.setattr(cccenter_catalog, "monotonic", lambda: result.now)
    monkeypatch.setattr(cccenter_catalog, "_perform_request", result.request)
    return result


@pytest.mark.parametrize("count", [0, 1, 10])
def test_selected_details_preserve_identity_and_order(
    transport: DetailTransport,
    count: int,
) -> None:
    selected = tuple(listing(i) for i in range(count))
    details = CCCenterCatalogClient().fetch_product_details(selected)
    assert transport.urls == [item.url for item in selected]
    assert [item.product_id for item in details] == [
        item.product_id for item in selected
    ]
    assert all(item.current_price == Decimal(90) for item in details)
    assert all(item.price_status == "scalar" for item in details)


def test_single_product_interface_uses_the_same_bounded_path(
    transport: DetailTransport,
) -> None:
    product = CCCenterCatalogClient().fetch_product_detail(listing(1))
    assert product.product_id == listing(1).product_id
    assert transport.urls == [listing(1).url]


def test_batch_accepts_selected_catalog_products(transport: DetailTransport) -> None:
    selected = listing(1)
    product = CCCenterProduct(
        product_id=selected.product_id,
        name=selected.name,
        url=selected.url,
        sku="",
        current_price=selected.current_price,
        original_price=None,
        image_url=None,
        categories=(),
        is_in_stock=True,
    )
    details = CCCenterCatalogClient().fetch_product_details((product,))
    assert details[0].product_id == product.product_id
    assert transport.urls == [product.url]


def test_batch_rejects_more_than_ten_before_fetching(
    transport: DetailTransport,
) -> None:
    with pytest.raises(FeedFetchError, match="DetailProductLimitExceeded"):
        CCCenterCatalogClient().fetch_product_details(
            tuple(listing(i) for i in range(11)),
        )
    assert transport.urls == []


@pytest.mark.parametrize(
    ("invalid", "error"),
    [
        (
            replace(listing(2), product_id=listing(1).product_id),
            "InvalidProductIdentity",
        ),
        (
            replace(listing(2), url="https://other.example/product/item-2/"),
            "InvalidProductUrl",
        ),
        (
            replace(listing(2), url=listing(2).url + "?secret=hidden"),
            "InvalidProductUrl",
        ),
        (
            replace(listing(2), url="https://user:secret@cccenter.mk/product/item-2/"),
            "InvalidProductUrl",
        ),
        (listing(1), "DuplicateProduct"),
    ],
)
def test_entire_selection_is_validated_before_first_request(
    transport: DetailTransport,
    invalid: CCCenterListing,
    error: str,
) -> None:
    with pytest.raises(FeedFetchError, match=error) as raised:
        CCCenterCatalogClient().fetch_product_details((listing(1), invalid))
    assert transport.urls == []
    assert "secret" not in str(raised.value)


def test_deadline_is_shared_and_later_transfers_get_only_remaining_time(
    transport: DetailTransport,
) -> None:
    transport.durations = [250, 51]
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        CCCenterCatalogClient().fetch_product_details(
            (listing(1), listing(2), listing(3)),
        )
    assert transport.timeouts == [300, 50]
    assert transport.urls == [listing(1).url, listing(2).url]


def test_detail_parsing_also_consumes_the_aggregate_deadline(
    monkeypatch: pytest.MonkeyPatch,
    transport: DetailTransport,
) -> None:
    original = cccenter_catalog.parse_product_detail

    def parse(
        document: BeautifulSoup,
        selected: CCCenterListing | CCCenterProduct,
    ) -> CCCenterProduct:
        product = original(document, selected)
        transport.now = 300
        return product

    monkeypatch.setattr(cccenter_catalog, "parse_product_detail", parse)
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        CCCenterCatalogClient().fetch_product_details((listing(1), listing(2)))
    assert transport.urls == [listing(1).url]


def test_aggregate_request_cap_is_not_reset_between_products(
    monkeypatch: pytest.MonkeyPatch,
    transport: DetailTransport,
) -> None:
    monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_REQUESTS", 1)
    with pytest.raises(FeedFetchError, match="RequestLimitExceeded"):
        CCCenterCatalogClient().fetch_product_details((listing(1), listing(2)))
    assert transport.urls == [listing(1).url]


def test_aggregate_bytes_are_not_reset_between_products(
    monkeypatch: pytest.MonkeyPatch,
    transport: DetailTransport,
) -> None:
    monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_SCAN_BYTES", len(DETAIL) + 1)
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        CCCenterCatalogClient().fetch_product_details(
            (listing(1), listing(2), listing(3)),
        )
    assert transport.urls == [listing(1).url, listing(2).url]


def test_shutdown_before_batch_makes_no_requests(transport: DetailTransport) -> None:
    with pytest.raises(FeedFetchInterruptedError):
        CCCenterCatalogClient().fetch_product_details(
            (listing(1),),
            is_shutdown_requested=lambda: True,
        )
    assert transport.urls == []


def test_shutdown_during_later_response_discards_partial_batch(
    transport: DetailTransport,
) -> None:
    with pytest.raises(FeedFetchInterruptedError):
        CCCenterCatalogClient().fetch_product_details(
            (listing(1), listing(2), listing(3)),
            is_shutdown_requested=lambda: len(transport.urls) >= 2,
        )
    assert transport.urls == [listing(1).url, listing(2).url]


def test_redirect_cannot_change_selected_source_identity(
    transport: DetailTransport,
) -> None:
    transport.response = Response(
        status_code=302,
        headers={"Location": listing(2).url},
    )
    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        CCCenterCatalogClient().fetch_product_details((listing(1),))
    assert transport.urls == [listing(1).url]
