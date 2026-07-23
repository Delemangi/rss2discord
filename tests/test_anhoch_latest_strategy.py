from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from rss2discord.models import SourceMetric
from rss2discord.transports import FeedFetchError
from rss2discord.transports.anhoch import AnhochStrategy
from tests.anhoch_helpers import (
    CATALOG_URL,
    RaisingGet,
    RecordingGet,
    RedirectingGet,
    StubResponse,
    page_payload,
    product_payload,
    requested_page_numbers,
)


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


def test_anhoch_strategy_fetches_three_page_latest_window_when_catalog_has_more_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(1, 99, [product_payload(3, "p-3")])),
            StubResponse(page_payload(2, 99, [product_payload(2, "p-2")])),
            StubResponse(page_payload(3, 99, [product_payload(1, "p-1")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    entries, _ = AnhochStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert [entry.id for entry in entries] == [1, 2, 3]
    assert requested_page_numbers(get.urls) == ["1", "2", "3"]


def test_anhoch_strategy_rejects_malformed_catalog_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b'{"products": {"data": "invalid"}}')])
    monkeypatch.setattr(requests, "get", get)
    strategy = AnhochStrategy()

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        strategy.fetch_entries(CATALOG_URL)


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
    strategy = AnhochStrategy()

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        strategy.fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is retryable


def test_anhoch_strategy_marks_timeout_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RaisingGet(requests.Timeout()))
    strategy = AnhochStrategy()

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        strategy.fetch_entries(CATALOG_URL)

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
    strategy = AnhochStrategy()

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        strategy.fetch_entries(CATALOG_URL)


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


def test_anhoch_strategy_rejects_oversized_redirect_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    redirect = StubResponse(
        b"ignored",
        status_code=302,
        headers={"Content-Length": "1048577", "Location": "/products"},
    )
    final = StubResponse(page_payload(1, 1, []))
    monkeypatch.setattr(requests, "get", RedirectingGet(redirect, final))
    strategy = AnhochStrategy()

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        strategy.fetch_entries(CATALOG_URL)


def test_anhoch_strategy_redacts_malformed_url_credentials() -> None:
    # Given
    credential = "sensitive-value"
    malformed_url = f"https://user:{credential}@℀.example.test/products"
    strategy = AnhochStrategy()

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        strategy.fetch_entries(malformed_url)

    # Then
    assert fetch_error.value.cause_type == "InvalidUrl"
    assert credential not in str(fetch_error.value)


def test_anhoch_strategy_restarts_latest_window_after_retryable_later_page_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(page_payload(1, 2, [product_payload(2, "new-product-0002")])),
            StubResponse(b"retry me", status_code=503),
            StubResponse(page_payload(1, 2, [product_payload(2, "new-product-0002")])),
            StubResponse(page_payload(2, 2, [product_payload(1, "old-product-0001")])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    strategy = AnhochStrategy()

    # When / Then
    with pytest.raises(FeedFetchError) as fetch_error:
        strategy.fetch_entries(CATALOG_URL)
    assert fetch_error.value.retryable

    entries, _ = strategy.fetch_entries(CATALOG_URL)
    assert [entry.id for entry in entries] == [1, 2]
    assert len(get.urls) == 4
    assert requested_page_numbers(get.urls) == ["1", "2", "1", "2"]


def test_anhoch_strategy_accepts_formatted_only_ancillary_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_payload(1, "product-0001")
    product["price"] = {"formatted": "1.500,00 ден."}
    product["installments"] = {
        "period": 24,
        "price": {"formatted": "50,00 ден."},
    }
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(page_payload(1, 1, [product]))]),
    )

    # When
    entries, _ = AnhochStrategy().fetch_entries(CATALOG_URL)
    entry = AnhochStrategy().get_entry_data(entries[0])

    # Then
    assert entry.source_metrics == (
        SourceMetric(label="Price", value="1.200,00 ден."),
        SourceMetric(label="Original", value="1.500,00 ден."),
        SourceMetric(label="Stock", value="7"),
        SourceMetric(label="Installments", value="24 × 50,00 ден."),
    )
