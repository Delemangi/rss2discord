from decimal import Decimal

import pytest
import requests
from pydantic import JsonValue

from rss2discord import transports
from rss2discord.models import SourceMetric
from rss2discord.price_amount import PriceAmountValidationError
from rss2discord.transports import FeedFetchError
from rss2discord.transports.setec import format_setec_mkd
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_LATEST_RESPONSE_BYTES,
    MAX_SETEC_REDIRECTS,
    SETEC_SEARCH_KEY,
    SETEC_SEARCH_URL,
    SETEC_STREAM_CHUNK_BYTES,
    SETEC_WINDOW_SIZE,
    SetecSearchRequest,
)
from rss2discord.transports.setec_http import SetecSearchClient
from tests.setec_helpers import (
    CATALOG_URL,
    RaisingPost,
    RecordingPost,
    StubResponse,
    product_payload,
    search_payload,
)

OLDEST_CREATED_AT = "2026-07-23T02:24:28.424Z"
NEWEST_CREATED_AT = "2026-07-24T09:11:03.001Z"
REDIRECT_BODY = b"r" * 40
FINAL_BODY = b"f" * 40


def redirect_response(location: str, body: bytes = b"") -> StubResponse:
    """Build a same-origin 302 carrying an optional body."""
    return StubResponse(body, status_code=302, headers={"Location": location})


def bounded_search_request(
    *,
    max_single_response_bytes: int,
    remaining_scan_response_bytes: int,
) -> SetecSearchRequest:
    """Build a minimal search request with explicit byte budgets."""
    return SetecSearchRequest(
        body={"q": ""},
        max_single_response_bytes=max_single_response_bytes,
        remaining_scan_response_bytes=remaining_scan_response_bytes,
    )


def dated_product_payload(
    product_id: str,
    handle: str,
    created_at: str,
    *,
    price: int | str = 1_499,
    original_price: int | str = 1_999,
) -> dict[str, JsonValue]:
    """Build a display-projection hit stamped with an explicit creation time."""
    hit = product_payload(
        product_id,
        handle,
        price=price,
        original_price=original_price,
    )
    hit["created_at"] = created_at
    return hit


def test_setec_strategy_fetches_latest_window_and_maps_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    newest_first_hits = [
        dated_product_payload(
            "prod-new-2",
            "new-product-2",
            NEWEST_CREATED_AT,
            price=999,
            original_price=999,
        ),
        dated_product_payload("prod-new-1", "new-product-1", OLDEST_CREATED_AT),
    ]
    post = RecordingPost([StubResponse(search_payload(newest_first_hits))])
    monkeypatch.setattr(requests, "post", post)
    strategy = transports.SetecStrategy()

    # When
    entries, source_title = strategy.fetch_entries(CATALOG_URL)
    data = strategy.get_entry_data(entries[0])

    # Then
    assert source_title == "Setec"
    assert strategy.seed_existing_on_first_fetch
    assert len(post.urls) == 1
    assert [strategy.get_entry_id(entry) for entry in entries] == [
        "prod-new-1",
        "prod-new-2",
    ]
    assert [entry.created_at.isoformat() for entry in entries] == [
        "2026-07-23T02:24:28.424000+00:00",
        "2026-07-24T09:11:03.001000+00:00",
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


def test_setec_strategy_issues_a_single_sorted_discovery_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [StubResponse(search_payload([product_payload("prod-1", "product-1")]))],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert post.urls == [SETEC_SEARCH_URL]
    body = post.bodies[0]
    assert body["sort"] == ["created_at:desc"]
    assert body["limit"] == SETEC_WINDOW_SIZE
    assert SETEC_WINDOW_SIZE == 100
    assert "offset" not in body
    assert "facets" not in body
    assert body["attributesToRetrieve"] == [
        "id",
        "title",
        "handle",
        "thumbnail",
        "created_at",
        "product_categories.name",
        "variants.calculated_price.calculated_amount",
        "variants.calculated_price.original_amount",
        "variants.calculated_price.currency_code",
    ]
    assert post.allow_redirects == [False]
    assert post.headers[0]["Authorization"] == f"Bearer {SETEC_SEARCH_KEY}"


def test_setec_strategy_reposts_the_body_on_a_same_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    redirect_url = f"{SETEC_SEARCH_URL}?redirected=1"
    post = RecordingPost(
        [
            StubResponse(b"", status_code=302, headers={"Location": redirect_url}),
            StubResponse(
                search_payload([product_payload("prod-1", "product-1")]),
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    entries, _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert [transports.SetecStrategy().get_entry_id(entry) for entry in entries] == [
        "prod-1",
    ]
    assert post.urls == [SETEC_SEARCH_URL, redirect_url]
    assert len(post.bodies) == 2
    assert post.bodies[0] == post.bodies[1]


def test_setec_strategy_redacts_the_search_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(b"failure", status_code=500)]),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert SETEC_SEARCH_KEY not in str(fetch_error.value)
    assert SETEC_SEARCH_KEY not in repr(fetch_error.value)


def test_setec_strategy_omits_original_price_when_not_discounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = product_payload("prod-1", "product-1", price=999, original_price=999)
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(search_payload([product]))]),
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
        "post",
        RecordingPost([StubResponse(search_payload([product]))]),
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
        "post",
        RecordingPost([StubResponse(search_payload([product]))]),
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
        "post",
        RecordingPost([StubResponse(search_payload([]))]),
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
    post = RecordingPost([StubResponse(b'{"hits": "invalid"}')])
    monkeypatch.setattr(requests, "post", post)

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
    post = RecordingPost([StubResponse(b"failure", status_code=status_code)])
    monkeypatch.setattr(requests, "post", post)

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
    monkeypatch.setattr(requests, "post", RaisingPost(requests.Timeout()))

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
        "post",
        RaisingPost(requests.exceptions.ChunkedEncodingError()),
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
    post = RecordingPost(
        [
            StubResponse(
                b"failure",
                status_code=429,
                headers={"Retry-After": "Thu, 23 Jul 2099 10:00:00 GMT"},
            ),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

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
    post = RecordingPost(
        [StubResponse(b"{}", headers={"Content-Length": "1048577"})],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)


def test_setec_strategy_rejects_an_undeclared_response_streamed_past_the_byte_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    chunk_total = MAX_SETEC_LATEST_RESPONSE_BYTES // SETEC_STREAM_CHUNK_BYTES + 1
    streamed_chunk = b"x" * SETEC_STREAM_CHUNK_BYTES
    assert chunk_total * SETEC_STREAM_CHUNK_BYTES > MAX_SETEC_LATEST_RESPONSE_BYTES
    post = RecordingPost(
        [StubResponse(b"", chunks=(streamed_chunk,) * chunk_total)],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert len(post.urls) == 1


def test_setec_strategy_rejects_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(
                b"",
                status_code=302,
                headers={"Location": "http://127.0.0.1/internal"},
            ),
            StubResponse(search_payload([])),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)
    assert len(post.urls) == 1


def test_setec_strategy_rejects_a_redirect_without_a_location_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            StubResponse(b"", status_code=302, headers={}),
            StubResponse(search_payload([product_payload("prod-1", "product-1")])),
        ],
    )
    monkeypatch.setattr(requests, "post", post)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.cause_type == "InvalidRedirect"
    assert post.urls == [SETEC_SEARCH_URL]


def test_setec_strategy_rejects_a_redirect_chain_longer_than_the_redirect_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    hops = [
        redirect_response(f"{SETEC_SEARCH_URL}?hop={hop}")
        for hop in range(MAX_SETEC_REDIRECTS + 1)
    ]
    post = RecordingPost([*hops, StubResponse(search_payload([]))])
    monkeypatch.setattr(requests, "post", post)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = transports.SetecStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert fetch_error.value.cause_type == "TooManyRedirects"
    assert MAX_SETEC_REDIRECTS == 10
    assert len(post.urls) == 11
    assert post.urls[0] == SETEC_SEARCH_URL
    assert post.urls[-1] == f"{SETEC_SEARCH_URL}?hop={MAX_SETEC_REDIRECTS - 1}"


def test_setec_search_client_charges_a_redirect_body_to_the_scan_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            redirect_response(f"{SETEC_SEARCH_URL}?redirected=1", REDIRECT_BODY),
            StubResponse(FINAL_BODY),
        ],
    )
    monkeypatch.setattr(requests, "post", post)
    request = bounded_search_request(
        max_single_response_bytes=len(FINAL_BODY),
        remaining_scan_response_bytes=len(REDIRECT_BODY) + len(FINAL_BODY) - 1,
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        _ = SetecSearchClient().search(SETEC_SEARCH_URL, request)

    # Then
    assert fetch_error.value.cause_type == "ScanResponseTooLarge"
    assert len(post.urls) == 2


def test_setec_search_client_counts_a_redirect_body_in_the_consumed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    post = RecordingPost(
        [
            redirect_response(f"{SETEC_SEARCH_URL}?redirected=1", REDIRECT_BODY),
            StubResponse(FINAL_BODY),
        ],
    )
    monkeypatch.setattr(requests, "post", post)
    request = bounded_search_request(
        max_single_response_bytes=len(FINAL_BODY),
        remaining_scan_response_bytes=len(REDIRECT_BODY) + len(FINAL_BODY),
    )

    # When
    fetched = SetecSearchClient().search(SETEC_SEARCH_URL, request)

    # Then
    assert fetched.content == FINAL_BODY
    assert fetched.response_bytes == len(REDIRECT_BODY) + len(FINAL_BODY)


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
