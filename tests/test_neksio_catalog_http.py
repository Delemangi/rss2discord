from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
import requests

from rss2discord.transports import FeedFetchError
from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from tests.neksio_helpers import (
    CATALOG_URL,
    RaisingPost,
    RecordingGet,
    RecordingPost,
    StubResponse,
    homepage_payload,
    page_payload,
    product_card,
)

_FILTER_URL = "https://g.store.neksio.mk/FilterAndPaginateProducts"


def _install_successful_catalog_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product_card(1)]))]),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://g.store.neksio.mk/",
        "https://catalog.example.test/",
        "https://g.store.neksio.mk:444/",
        "https://user:top-secret@g.store.neksio.mk/",
    ],
)
def test_fetch_catalog_rejects_non_first_party_origins(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    # Given
    _install_successful_catalog_transport(monkeypatch)

    # When / Then
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(url)

    assert fetch_error.value.cause_type == "InvalidUrl"
    assert "top-secret" not in str(fetch_error.value)


@pytest.mark.parametrize(
    "location",
    [
        "https://catalog.example.test/",
        "http://g.store.neksio.mk/",
        "https://g.store.neksio.mk:444/",
        "https://user:top-secret@g.store.neksio.mk/",
    ],
)
def test_fetch_catalog_rejects_cross_origin_scheme_port_and_credential_redirects(
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b"", status_code=302, headers={"Location": location})])
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidRedirect") as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    assert len(get.urls) == 1
    assert "top-secret" not in str(fetch_error.value)


def test_fetch_catalog_follows_same_origin_redirects_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet(
        [
            StubResponse(b"", status_code=302, headers={"Location": "/"}),
            StubResponse(homepage_payload([1])),
        ],
    )
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product_card(1)]))]),
    )

    # When
    products = NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert [product.product_id for product in products] == [1]
    assert get.urls == ["https://g.store.neksio.mk/", "https://g.store.neksio.mk/"]


def test_fetch_catalog_rejects_redirects_past_the_fixed_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    redirects = [StubResponse(b"", status_code=302, headers={"Location": "/"})] * 11
    get = RecordingGet(redirects)
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="TooManyRedirects"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    assert len(get.urls) == 11


def test_fetch_catalog_rejects_oversized_homepage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(b"", headers={"Content-Length": "1048577"})]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_rejects_oversized_streamed_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(b"", chunks=(b"x" * 1_048_576, b"y"))]),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)


def test_fetch_catalog_classifies_numeric_retry_after_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(b"failure", status_code=429, headers={"Retry-After": "2.5"})]),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert fetch_error.value.retry_after == 2.5


def test_fetch_catalog_classifies_http_date_retry_after_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    retry_at = datetime(2099, 7, 26, 15, 0, tzinfo=UTC)
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(
                    b"failure",
                    status_code=429,
                    headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
                ),
            ],
        ),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert fetch_error.value.retry_after is not None
    assert fetch_error.value.retry_after > 0


@pytest.mark.parametrize("error", [requests.ConnectionError(), requests.Timeout()])
def test_fetch_catalog_marks_request_transport_interruptions_retryable(
    monkeypatch: pytest.MonkeyPatch,
    error: requests.RequestException,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(requests, "post", RaisingPost(error))

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert fetch_error.value.retryable
    assert fetch_error.value.cause_type == type(error).__name__


def test_fetch_catalog_marks_stream_interruptions_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RecordingGet([StubResponse(homepage_payload([1]))]))
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost(
            [
                StubResponse(
                    b"",
                    interruption=requests.exceptions.ChunkedEncodingError(),
                ),
            ],
        ),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        NeksioCatalogClient().fetch_catalog(CATALOG_URL)

    # Then
    assert fetch_error.value.retryable
    assert fetch_error.value.cause_type == "ChunkedEncodingError"
