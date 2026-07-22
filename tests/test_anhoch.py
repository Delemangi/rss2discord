import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from rss2discord.models import SourceMetric
from rss2discord.transports import FeedFetchError
from rss2discord.transports.anhoch import AnhochStrategy

CATALOG_URL = (
    "https://www.anhoch.com/products?query=keyboard&inStockOnly=2&sort=price_asc"
)


def product_payload(
    product_id: int,
    slug: str,
    *,
    selling_price: str = "1.200,00 ден.",
) -> dict[str, object]:
    return {
        "id": product_id,
        "name": f"Product {product_id}",
        "slug": slug,
        "price": {"formatted": "1.500,00 ден."},
        "selling_price": {"formatted": selling_price},
        "base_image": {"path": f"https://www.anhoch.com/images/{product_id}.jpg"},
        "is_in_stock": True,
        "qty": 7,
        "installments": {
            "period": 24,
            "price": {"formatted": "50,00 ден."},
        },
    }


def page_payload(
    current_page: int,
    last_page: int,
    products: list[dict[str, object]],
) -> bytes:
    return json.dumps(
        {
            "products": {
                "current_page": current_page,
                "last_page": last_page,
                "data": products,
            },
        },
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield self.content


class RecordingGet:
    """Return queued responses while recording mutable request history."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
    ) -> AbstractContextManager[StubResponse]:
        del timeout, stream
        self.urls.append(url)
        self.headers.append(headers)
        return nullcontext(self.responses.pop(0))


@dataclass(frozen=True, slots=True)
class RaisingGet:
    error: requests.RequestException

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, stream
        raise self.error


def test_anhoch_strategy_fetches_pages_and_maps_products_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(2, "new-product-0002")])),
            StubResponse(page_payload(2, 2, [product_payload(1, "old-product-0001")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    strategy = AnhochStrategy()

    # When
    entries, source_title = strategy.fetch_entries(CATALOG_URL)
    data = strategy.get_entry_data(entries[0])

    # Then
    assert source_title == "Anhoch"
    assert [strategy.get_entry_id(entry) for entry in entries] == ["1", "2"]
    assert data.title == "Product 1"
    assert data.link == "https://www.anhoch.com/products/old-product-0001"
    assert data.description == ""
    assert data.author == ""
    assert data.timestamp is not None
    assert datetime.fromisoformat(data.timestamp).tzinfo is not None
    assert data.image_url == "https://www.anhoch.com/images/1.jpg"
    assert data.source_metrics == (
        SourceMetric(label="Price", value="1.200,00 ден."),
        SourceMetric(label="Original", value="1.500,00 ден."),
        SourceMetric(label="Stock", value="7"),
        SourceMetric(label="Installments", value="24 × 50,00 ден."),
    )
    assert len(get.urls) == 2
    for page, requested_url in enumerate(get.urls, start=1):
        query = parse_qs(urlsplit(requested_url).query)
        assert query["query"] == ["keyboard"]
        assert query["inStockOnly"] == ["2"]
        assert query["sort"] == ["latest"]
        assert query["perPage"] == ["30"]
        assert query["page"] == [str(page)]
    assert all(headers["Accept"] == "application/json" for headers in get.headers)
    assert all(
        headers["X-Requested-With"] == "XMLHttpRequest" for headers in get.headers
    )


def test_anhoch_strategy_bounds_catalog_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(page, 99, [product_payload(page, f"p-{page}")]))
            for page in range(1, 4)
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    entries, _ = AnhochStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert len(get.urls) == 3
    assert [entry.id for entry in entries] == [3, 2, 1]


def test_anhoch_strategy_rejects_malformed_catalog_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b'{"products": {"data": "invalid"}}')])
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        AnhochStrategy().fetch_entries(CATALOG_URL)


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(404, False), (429, True), (503, True)],
)
def test_anhoch_strategy_classifies_http_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    retryable: bool,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b"failure", status_code=status_code)])
    monkeypatch.setattr(requests, "get", get)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        AnhochStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is retryable


def test_anhoch_strategy_marks_timeout_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RaisingGet(requests.Timeout()))

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        AnhochStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.retryable


def test_anhoch_strategy_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                b"{}",
                headers={"Content-Length": "1048577"},
            ),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        AnhochStrategy().fetch_entries(CATALOG_URL)


def test_anhoch_strategy_accepts_empty_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(page_payload(1, 1, []))])
    monkeypatch.setattr(requests, "get", get)

    # When
    entries, source_title = AnhochStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert entries == []
    assert source_title == "Anhoch"


def test_anhoch_strategy_accepts_empty_image_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_payload(1, "product-without-image")
    product["base_image"] = []
    get = RecordingGet([StubResponse(page_payload(1, 1, [product]))])
    monkeypatch.setattr(requests, "get", get)

    # When
    entries, _ = AnhochStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert AnhochStrategy().get_entry_data(entries[0]).image_url is None
