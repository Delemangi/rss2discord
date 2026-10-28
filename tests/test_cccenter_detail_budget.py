import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.cccenter import catalog as cccenter_catalog
from rss2discord.providers.cccenter.catalog import CCCenterCatalogClient
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from rss2discord.retries import FeedFetchInterruptedError

DETAIL_PRICE = "9000"


def listing(number: int) -> CCCenterListing:
    url = f"https://cccenter.mk/product/item-{number}/"
    return CCCenterListing(
        url,
        "Selected product",
        url,
        Decimal(90),
        None,
        None,
        store_api_id=number + 100,
    )


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
    response: Response = field(
        default_factory=lambda: Response(
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-WP-Total": "1",
                "X-WP-TotalPages": "1",
            },
        ),
    )
    detail_price: str = DETAIL_PRICE
    detail_count: int = 1
    returned_url: str | None = None
    returned_id: int | None = None
    response_slug: str | None = None
    malformed_body: bytes | None = None

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
        api_id = int(parse_qs(urlsplit(url).query).get("include", ["0"])[0])
        slug = self.response_slug or f"item-{api_id - 100}"
        payload = [
            {
                "id": self.returned_id if self.returned_id is not None else api_id,
                "slug": slug,
                "permalink": self.returned_url
                or f"https://cccenter.mk/product/{slug}/",
                "name": "Selected product",
                "sku": "",
                "images": [],
                "categories": [],
                "is_in_stock": True,
                "type": "simple",
                "has_options": False,
                "prices": {
                    "currency_code": "MKD",
                    "currency_minor_unit": 2,
                    "price": self.detail_price,
                    "regular_price": self.detail_price,
                    "price_range": None,
                },
            },
        ]
        payload = payload[: self.detail_count]
        content_callback(self.malformed_body or json.dumps(payload).encode())
        self.response.headers = {
            **self.response.headers,
            "X-WP-Total": str(self.detail_count),
            "X-WP-TotalPages": "1",
        }
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
    assert len(transport.urls) == len(selected)
    assert all("rest_route=/wc/store/v1/products" in url for url in transport.urls)
    assert all("include=" in url and "slug=" not in url for url in transport.urls)
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
    assert len(transport.urls) == 1
    assert "include=101" in transport.urls[0]


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
        store_api_id=selected.store_api_id,
    )
    details = CCCenterCatalogClient().fetch_product_details((product,))
    assert details[0].product_id == product.product_id
    assert len(transport.urls) == 1
    assert "include=101" in transport.urls[0]


@pytest.mark.parametrize(
    ("response_update", "error"),
    [
        (
            {"returned_url": "https://cccenter.mk/product/item-2/"},
            "InvalidProductIdentity",
        ),
        ({"returned_id": 999}, "InvalidProductIdentity"),
        ({"detail_count": 0}, "InvalidDetail"),
        ({"detail_count": 2}, "InvalidDetail"),
        ({"malformed_body": b"{"}, "InvalidDetail"),
    ],
)
def test_detail_requires_one_well_formed_exact_url_match(
    transport: DetailTransport,
    response_update: dict[str, object],
    error: str,
) -> None:
    for key, value in response_update.items():
        setattr(transport, key, value)
    client = CCCenterCatalogClient()
    selected_listing = listing(1)
    with pytest.raises(FeedFetchError, match=error):
        client.fetch_product_detail(selected_listing)


def test_detail_accepts_zero_as_unpriced_scalar_and_encodes_unicode_slug(
    transport: DetailTransport,
) -> None:
    transport.detail_price = "0"
    result = CCCenterCatalogClient().fetch_product_detail(listing(1))
    assert result.current_price == Decimal(0)

    encoded_url = "https://cccenter.mk/product/ssd-2-5%e2%80%b3-500gb/"
    transport.returned_url = encoded_url
    transport.response_slug = "ssd-2-5″-500gb"
    selected = CCCenterListing(
        encoded_url,
        "SSD",
        encoded_url,
        Decimal(90),
        None,
        None,
        store_api_id=501,
    )
    product = CCCenterCatalogClient().fetch_product_detail(selected)
    assert product.product_id == encoded_url
    query = parse_qs(urlsplit(transport.urls[-1]).query)
    assert query["include"] == ["501"]
    assert "slug" not in query


def test_detail_response_price_and_locator_remain_available_for_confirmation(
    transport: DetailTransport,
) -> None:
    transport.detail_price = "9012"
    selected = listing(1)
    detail = CCCenterCatalogClient().fetch_product_detail(selected)
    assert detail.product_id == selected.url
    assert detail.store_api_id == selected.store_api_id
    assert detail.current_price == Decimal("90.12")


def test_batch_rejects_more_than_ten_before_fetching(
    transport: DetailTransport,
) -> None:
    client = CCCenterCatalogClient()
    listings = tuple(listing(i) for i in range(11))
    with pytest.raises(FeedFetchError, match="DetailProductLimitExceeded"):
        client.fetch_product_details(
            listings,
        )
    assert transport.urls == []


@pytest.mark.parametrize(
    ("invalid", "error"),
    [
        (
            replace(listing(2), product_id=listing(1).product_id),
            "InvalidProductIdentity",
        ),
        (replace(listing(2), store_api_id=None), "InvalidProductLocator"),
        (replace(listing(2), store_api_id=-2), "InvalidProductLocator"),
        (replace(listing(2), store_api_id=True), "InvalidProductLocator"),
        (replace(listing(2), store_api_id=listing(1).store_api_id), "DuplicateProduct"),
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
    client = CCCenterCatalogClient()
    listings = (listing(1), invalid)
    with pytest.raises(FeedFetchError, match=error) as raised:
        client.fetch_product_details(listings)
    assert transport.urls == []
    assert "secret" not in str(raised.value)


def test_deadline_is_shared_and_later_transfers_get_only_remaining_time(
    transport: DetailTransport,
) -> None:
    transport.durations = [250, 51]
    client = CCCenterCatalogClient()
    listings = (listing(1), listing(2), listing(3))
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        client.fetch_product_details(
            listings,
        )
    assert transport.timeouts == [300, 50]
    assert len(transport.urls) == 2


def test_detail_parsing_also_consumes_the_aggregate_deadline(
    monkeypatch: pytest.MonkeyPatch,
    transport: DetailTransport,
) -> None:
    original = CCCenterCatalogClient._store_product

    def parse(
        item: object,
        selected: set[tuple[str, str | int]],
    ) -> CCCenterProduct:
        product = original(item, selected)
        transport.now = 300
        return product

    monkeypatch.setattr(CCCenterCatalogClient, "_store_product", staticmethod(parse))
    client = CCCenterCatalogClient()
    listings = (listing(1), listing(2))
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        client.fetch_product_details(listings)
    assert len(transport.urls) == 1


def test_aggregate_request_cap_is_not_reset_between_products(
    monkeypatch: pytest.MonkeyPatch,
    transport: DetailTransport,
) -> None:
    monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_REQUESTS", 1)
    client = CCCenterCatalogClient()
    listings = (listing(1), listing(2))
    with pytest.raises(FeedFetchError, match="RequestLimitExceeded"):
        client.fetch_product_details(listings)
    assert len(transport.urls) == 1


def test_aggregate_bytes_are_not_reset_between_products(
    monkeypatch: pytest.MonkeyPatch,
    transport: DetailTransport,
) -> None:
    monkeypatch.setattr(cccenter_catalog, "MAX_CCCENTER_SCAN_BYTES", 400)
    client = CCCenterCatalogClient()
    listings = (listing(1), listing(2), listing(3))
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        client.fetch_product_details(
            listings,
        )
    assert len(transport.urls) == 2


def test_shutdown_before_batch_makes_no_requests(transport: DetailTransport) -> None:
    client = CCCenterCatalogClient()
    listings = (listing(1),)
    with pytest.raises(FeedFetchInterruptedError):
        client.fetch_product_details(
            listings,
            is_shutdown_requested=lambda: True,
        )
    assert transport.urls == []


def test_shutdown_during_later_response_discards_partial_batch(
    transport: DetailTransport,
) -> None:
    client = CCCenterCatalogClient()
    listings = (listing(1), listing(2), listing(3))
    with pytest.raises(FeedFetchInterruptedError):
        client.fetch_product_details(
            listings,
            is_shutdown_requested=lambda: len(transport.urls) >= 2,
        )
    assert len(transport.urls) == 2


def test_redirect_cannot_change_selected_source_identity(
    transport: DetailTransport,
) -> None:
    transport.response = Response(
        status_code=302,
        headers={"Location": listing(2).url},
    )
    client = CCCenterCatalogClient()
    listings = (listing(1),)
    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        client.fetch_product_details(listings)
    assert len(transport.urls) == 1
