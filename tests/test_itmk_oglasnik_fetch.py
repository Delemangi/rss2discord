import traceback
from collections.abc import Mapping

import pytest
import requests

from rss2discord.transports import FeedFetchError
from rss2discord.transports.itmk_oglasnik_http import fetch_itmk_oglasnik_page
from tests.itmk_oglasnik_fixtures import (
    COMPLETE_CARD,
    INDEX_URL,
    RaisingGet,
    StubGet,
    StubResponse,
    make_response,
)


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(404, False), (429, True), (503, True)],
)
def test_itmk_oglasnik_strategy_classifies_http_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    retryable: bool,
) -> None:
    # Given
    response = make_response("failure", status_code)
    monkeypatch.setattr(requests, "get", StubGet(response))

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_itmk_oglasnik_page(INDEX_URL)

    # Then
    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is retryable
    rendered_error = "".join(
        traceback.format_exception(
            fetch_error.type,
            fetch_error.value,
            fetch_error.tb,
        ),
    )
    assert "secret-token" not in rendered_error


def test_itmk_oglasnik_strategy_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = StubResponse(
        text="failure",
        status_code=429,
        headers={"Retry-After": "17"},
    )
    monkeypatch.setattr(requests, "get", StubGet(response))

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_itmk_oglasnik_page(INDEX_URL)

    # Then
    assert fetch_error.value.retry_after == 17


@pytest.mark.parametrize(
    ("request_error", "retryable"),
    [
        (requests.ConnectionError(INDEX_URL), True),
        (requests.Timeout(INDEX_URL), True),
        (requests.RequestException(INDEX_URL), False),
    ],
)
def test_itmk_oglasnik_strategy_classifies_request_failures_without_url_leakage(
    monkeypatch: pytest.MonkeyPatch,
    request_error: requests.RequestException,
    retryable: bool,
) -> None:
    # Given
    monkeypatch.setattr(requests, "get", RaisingGet(request_error))

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_itmk_oglasnik_page(INDEX_URL)

    # Then
    assert fetch_error.value.retryable is retryable
    assert "secret-token" not in str(fetch_error.value)


def test_itmk_oglasnik_strategy_rejects_declared_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = StubResponse(
        text=f"<h1>Огласник</h1>{COMPLETE_CARD}",
        status_code=200,
        headers={"Content-Length": "1048577"},
    )
    monkeypatch.setattr(requests, "get", StubGet(response))

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        fetch_itmk_oglasnik_page(INDEX_URL)


@pytest.mark.parametrize(
    ("status_code", "headers"),
    [(200, {}), (302, {"Location": "/oglasnik/"})],
    ids=["final-response", "redirect-response"],
)
def test_itmk_oglasnik_strategy_rejects_streamed_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    headers: Mapping[str, str],
) -> None:
    # Given
    response = StubResponse(
        text=f"<h1>Огласник</h1>{COMPLETE_CARD}",
        status_code=status_code,
        headers=headers,
        chunks=(b"x" * 600_000, b"x" * 600_000),
    )
    monkeypatch.setattr(requests, "get", StubGet(response))

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        fetch_itmk_oglasnik_page(INDEX_URL)
