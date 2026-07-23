import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from rss2discord import transports
from rss2discord.models import SourceMetric
from rss2discord.transports import FeedFetchError

CATALOG_URL = "https://setec.mk/e-prodazba"


def product_payload(
    product_id: str,
    handle: str,
    *,
    price: int = 1_499,
    original_price: int = 1_999,
) -> dict[str, object]:
    return {
        "id": product_id,
        "title": f"Product {product_id}",
        "handle": handle,
        "thumbnail": f"https://cdn.setec.mk/{product_id}.webp",
        "created_at": "2026-07-23T02:24:28.424Z",
        "variants": [
            {
                "calculated_price": {
                    "calculated_amount": price,
                    "original_amount": original_price,
                    "currency_code": "mkd",
                },
            },
        ],
        "categories": [{"name": "Computers"}, {"name": "Accessories"}],
    }


def catalog_payload(count: int, products: list[dict[str, object]]) -> bytes:
    return json.dumps({"count": count, "products": products}).encode()


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
        self.responses: list[StubResponse] = responses
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []
        self.allow_redirects: list[bool] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del timeout, stream
        self.urls.append(url)
        self.headers.append(headers)
        self.allow_redirects.append(allow_redirects)
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
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, stream, allow_redirects
        raise self.error


def test_setec_strategy_fetches_latest_window_and_maps_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_product = product_payload("prod-old", "old-product")
    latest_products = [
        product_payload("prod-new-1", "new-product-1"),
        product_payload("prod-new-2", "new-product-2", price=999, original_price=999),
    ]
    get = RecordingGet(
        [
            StubResponse(catalog_payload(35, [first_product])),
            StubResponse(catalog_payload(35, latest_products)),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    strategy = transports.SetecStrategy()

    # When
    entries, source_title = strategy.fetch_entries(CATALOG_URL)
    data = strategy.get_entry_data(entries[0])

    # Then
    assert source_title == "Setec"
    assert strategy.seed_existing_on_first_fetch
    assert [strategy.get_entry_id(entry) for entry in entries] == [
        "prod-new-1",
        "prod-new-2",
    ]
    assert data.title == "Product prod-new-1"
    assert data.link == "https://setec.mk/products/new-product-1"
    assert data.description == ""
    assert data.author == ""
    assert data.timestamp == "2026-07-23T02:24:28.424000+00:00"
    assert data.image_url == "https://cdn.setec.mk/prod-new-1.webp"
    assert data.categories == ("Computers", "Accessories")
    assert data.source_metrics == (
        SourceMetric(label="Price", value="1.499 ден."),
        SourceMetric(label="Original", value="1.999 ден."),
    )
    assert len(get.urls) == 2
    first_query = parse_qs(urlsplit(get.urls[0]).query)
    latest_query = parse_qs(urlsplit(get.urls[1]).query)
    assert urlsplit(get.urls[0]).path == "/api/medusa/products/list"
    assert first_query == {"limit": ["1"], "offset": ["0"], "region_id": ["mk"]}
    assert latest_query == {"limit": ["30"], "offset": ["5"], "region_id": ["mk"]}
    assert all(headers["Accept"] == "application/json" for headers in get.headers)
    assert get.allow_redirects == [False, False]


def test_setec_strategy_omits_original_price_when_not_discounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_payload("prod-1", "product-1", price=999, original_price=999)
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(catalog_payload(1, [product]))]),
    )

    # When
    entries, _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)
    data = transports.SetecStrategy().get_entry_data(entries[0])

    # Then
    assert data.source_metrics == (SourceMetric(label="Price", value="999 ден."),)


def test_setec_strategy_accepts_empty_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(catalog_payload(0, []))]),
    )

    # When
    entries, source_title = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert entries == []
    assert source_title == "Setec"


def test_setec_strategy_rejects_malformed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b'{"count": "invalid", "products": []}')])
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(404, False), (429, True), (503, True)],
)
def test_setec_strategy_classifies_http_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    retryable: bool,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b"failure", status_code=status_code)])
    monkeypatch.setattr(requests, "get", get)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is retryable


def test_setec_strategy_marks_timeout_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RaisingGet(requests.Timeout()))

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.retryable


def test_setec_strategy_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [StubResponse(b"{}", headers={"Content-Length": "1048577"})],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)


def test_setec_strategy_redacts_malformed_url_credentials() -> None:
    # Given
    credential = "sensitive-value"
    malformed_url = f"https://user:{credential}@℀.example.test/products"

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(malformed_url)

    # Then
    assert fetch_error.value.cause_type == "InvalidUrl"
    assert credential not in str(fetch_error.value)
