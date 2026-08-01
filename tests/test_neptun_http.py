import pytest
import requests

from rss2discord.transports import FeedFetchError
from rss2discord.transports.neptun_http import NeptunHttpClient, NeptunPageRequest
from tests.neptun_helpers import (
    CATEGORY_URL,
    RecordingRequests,
    StubResponse,
    TruncatedResponse,
    category_html,
    product_payload,
    products_payload,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CATEGORY_URL, CATEGORY_URL),
        ("https://neptun.mk/KOMPJUTERI.nspx", CATEGORY_URL),
    ],
)
def test_category_url_is_normalized_only_for_exact_neptun_hosts(
    url: str,
    expected: str,
) -> None:
    assert NeptunHttpClient().normalize_category_url(url) == expected


def test_category_url_rejects_query_filters() -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        NeptunHttpClient().normalize_category_url(
            "https://www.neptun.mk/KOMPJUTERI.nspx?brand=Lenovo",
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.neptun.mk/KOMPJUTERI.nspx",
        "https://evil.example/KOMPJUTERI.nspx",
        "https://www.neptun.mk.evil.example/KOMPJUTERI.nspx",
        "https://user:pass@www.neptun.mk/KOMPJUTERI.nspx",
        "https://www.neptun.mk:444/KOMPJUTERI.nspx",
        "https://www.neptun.mk/KOMPJUTERI.nspx#fragment",
    ],
)
def test_category_url_rejects_unsafe_authorities(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        NeptunHttpClient().normalize_category_url(url)


def test_category_page_parses_exactly_one_initial_search_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests_spy = RecordingRequests([StubResponse(category_html())])
    monkeypatch.setattr(requests, "get", requests_spy.get)

    model, response_bytes = NeptunHttpClient().fetch_category_model(CATEGORY_URL)

    assert (model.category_id, model.current_page, response_bytes) == (
        2,
        4,
        len(category_html()),
    )


@pytest.mark.parametrize(
    "content",
    [
        category_html(models=0),
        category_html(models=2),
        b"<div id='angularApp' data-initialsearchmodel='not-json'>",
    ],
)
def test_category_page_rejects_missing_malformed_or_ambiguous_models(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    monkeypatch.setattr(requests, "get", RecordingRequests([StubResponse(content)]).get)

    with pytest.raises(FeedFetchError, match="InvalidCategoryModel"):
        NeptunHttpClient().fetch_category_model(CATEGORY_URL)


def test_products_request_uses_observed_payload_and_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests_spy = RecordingRequests(
        [StubResponse(products_payload(1, [product_payload(7)]))],
    )
    monkeypatch.setattr(requests, "post", requests_spy.post)

    fetched = NeptunHttpClient().fetch_products(
        category_url=CATEGORY_URL,
        category_id=2,
        request=NeptunPageRequest(
            page=1,
            page_size=20,
            sort=7,
            remaining_scan_bytes=5 * 1024 * 1024,
        ),
    )

    assert fetched.response.batch.items[0].id == 7
    assert requests_spy.calls[0][2] == {
        "model": {
            "TotalItems": 0,
            "CurrentPage": 1,
            "ItemsPerPage": 20,
            "Sort": 7,
            "CategoryId": 2,
            "Recomended": False,
            "ShowAllProducts": True,
        },
    }


def test_products_request_rejects_off_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        RecordingRequests(
            [
                StubResponse(
                    b"redirect",
                    status_code=302,
                    headers={"Location": "https://evil.example/steal"},
                ),
            ],
        ).post,
    )

    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        NeptunHttpClient().fetch_products(
            category_url=CATEGORY_URL,
            category_id=2,
            request=NeptunPageRequest(
                page=1,
                page_size=20,
                sort=7,
                remaining_scan_bytes=5 * 1024 * 1024,
            ),
        )


def test_products_request_rejects_response_over_five_mib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        RecordingRequests([StubResponse(b"x" * (5 * 1024 * 1024 + 1))]).post,
    )

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        NeptunHttpClient().fetch_products(
            category_url=CATEGORY_URL,
            category_id=2,
            request=NeptunPageRequest(
                page=1,
                page_size=20,
                sort=7,
                remaining_scan_bytes=6 * 1024 * 1024,
            ),
        )


@pytest.mark.parametrize(
    "content",
    [b"not-json", b'{"Batch":{"Config":{"TotalItems":1},"Items":[{}]}}'],
)
def test_products_request_rejects_malformed_external_data(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        RecordingRequests([StubResponse(content)]).post,
    )

    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        NeptunHttpClient().fetch_products(
            category_url=CATEGORY_URL,
            category_id=2,
            request=NeptunPageRequest(
                page=1,
                page_size=20,
                sort=7,
                remaining_scan_bytes=5 * 1024 * 1024,
            ),
        )


def test_products_request_rejects_total_scan_byte_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        RecordingRequests([StubResponse(b"1234")]).post,
    )

    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        NeptunHttpClient().fetch_products(
            category_url=CATEGORY_URL,
            category_id=2,
            request=NeptunPageRequest(
                page=1,
                page_size=20,
                sort=7,
                remaining_scan_bytes=3,
            ),
        )


def test_products_request_classifies_truncated_stream_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        requests,
        "post",
        RecordingRequests([TruncatedResponse(b'{"Batch":')]).post,
    )

    with pytest.raises(FeedFetchError) as error:
        NeptunHttpClient().fetch_products(
            category_url=CATEGORY_URL,
            category_id=2,
            request=NeptunPageRequest(
                page=1,
                page_size=20,
                sort=7,
                remaining_scan_bytes=5 * 1024 * 1024,
            ),
        )

    assert error.value.cause_type == "ChunkedEncodingError"
    assert error.value.retryable
