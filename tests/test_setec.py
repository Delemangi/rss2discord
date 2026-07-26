from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from rss2discord import transports
from rss2discord.models import SourceMetric
from rss2discord.price_amount import PriceAmountValidationError
from rss2discord.transports import FeedFetchError
from rss2discord.transports.setec import format_setec_mkd
from tests.setec_helpers import (
    CATALOG_URL,
    RaisingGet,
    RecordingGet,
    StubResponse,
    catalog_payload,
    product_payload,
)


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


def test_setec_strategy_accepts_a_fractional_live_calculated_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_payload(
        "prod-fractional",
        "fractional-product",
        price=651_261.49217128,
        original_price=651_261.49217128,
    )
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(catalog_payload(1, [product]))]),
    )

    # When
    entries, _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)
    data = transports.SetecStrategy().get_entry_data(entries[0])

    # Then
    assert data.source_metrics == (
        SourceMetric(label="Price", value="651.261,49217128 ден."),
    )


def test_format_setec_mkd_trims_insignificant_fractional_zeroes() -> None:
    # Given
    amount = Decimal("1.2300")

    # When
    formatted_amount = format_setec_mkd(amount)

    # Then
    assert formatted_amount == "1,23 ден."


def test_format_setec_mkd_rejects_hostile_exponent_before_fixed_point_expansion() -> (
    None
):
    # Given
    amount = Decimal("1E+1000000")

    # When / Then
    with pytest.raises(PriceAmountValidationError):
        format_setec_mkd(amount)


@pytest.mark.parametrize(
    ("price", "original_price"),
    [
        ("1E+1000000", 1_499),
        (1_499, "1E+1000000"),
    ],
)
def test_setec_strategy_rejects_compact_hostile_price_as_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
    price: int | str,
    original_price: int | str,
) -> None:
    # Given
    product = product_payload(
        "prod-hostile",
        "hostile-product",
        price=price,
        original_price=original_price,
    )
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(catalog_payload(1, [product]))]),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.cause_type == "InvalidResponse"


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
    [(404, False), (408, True), (429, True), (503, True)],
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


def test_setec_strategy_marks_chunked_response_interruption_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        requests,
        "get",
        RaisingGet(requests.exceptions.ChunkedEncodingError()),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.retryable


def test_setec_strategy_parses_http_date_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                b"failure",
                status_code=429,
                headers={"Retry-After": "Thu, 23 Jul 2099 10:00:00 GMT"},
            ),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.retry_after is not None
    assert fetch_error.value.retry_after > 0


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


def test_setec_strategy_rejects_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(
                b"",
                status_code=302,
                headers={"Location": "http://127.0.0.1/internal"},
            ),
            StubResponse(catalog_payload(0, [])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)
    assert len(get.urls) == 1


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
