from collections.abc import Callable, Mapping

import pytest
from curl_cffi import CurlECode
from curl_cffi import requests as curl_requests

from rss2discord.transports import FeedFetchError, reklama5_http
from rss2discord.transports.reklama5_http import (
    MAX_REKLAMA5_ATTEMPT_BYTES,
    MAX_REKLAMA5_RESPONSE_BYTES,
    Reklama5ScanBudget,
    fetch_reklama5_page,
)
from tests.reklama5_helpers import RecordingGet, StubResponse, scan_budget, search_scope


class CallbackSession:
    def __init__(
        self,
        response: StubResponse | None = None,
        error: curl_requests.RequestsError | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.timeout_ms: list[int] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> StubResponse:
        del url, headers, allow_redirects
        self.timeout_ms.append(timeout_ms)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        for chunk in self.response.chunks or (self.response.content,):
            content_callback(chunk)
        return self.response

    def close(self) -> None:
        pass


def test_reklama5_fetch_uses_curl_total_timeout_in_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CallbackSession(StubResponse(b"page"))
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: session)
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10.0)

    content = fetch_reklama5_page(
        search_scope().page_request(1),
        scan_budget(expires_at=15.0),
    )

    assert content == b"page"
    assert session.timeout_ms == [5_000]


def test_reklama5_fetch_classifies_curl_timeout_at_deadline_as_scan_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CallbackSession(
        error=curl_requests.RequestsError(
            "operation timed out",
            CurlECode.OPERATION_TIMEDOUT,
        ),
    )
    monotonic_values = iter((0.0, 1.0))
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: session)
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(
            search_scope().page_request(1),
            scan_budget(expires_at=1.0),
        )

    assert fetch_error.value.cause_type == "ScanTimeLimitExceeded"
    assert not fetch_error.value.retryable


def test_reklama5_fetch_uses_remaining_deadline_as_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b"page")])
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10)

    fetch_reklama5_page(
        search_scope().page_request(1),
        scan_budget(bytes_remaining=100, expires_at=15),
    )

    assert get.timeouts == [5]


@pytest.mark.parametrize(
    "response",
    [
        StubResponse(
            b"",
            headers={"Content-Length": str(MAX_REKLAMA5_RESPONSE_BYTES + 1)},
        ),
        StubResponse(b"", chunks=(b"x" * MAX_REKLAMA5_RESPONSE_BYTES, b"y")),
        StubResponse(
            b"",
            status_code=302,
            headers={"Location": search_scope().page_request(1).url},
            url=search_scope().page_request(1).url,
            chunks=(b"x" * MAX_REKLAMA5_RESPONSE_BYTES, b"y"),
        ),
    ],
)
def test_reklama5_fetch_rejects_declared_and_streamed_oversized_bodies(
    monkeypatch: pytest.MonkeyPatch,
    response: StubResponse,
) -> None:
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([response]),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == "ResponseTooLarge"
    assert not fetch_error.value.retryable


def test_reklama5_fetch_charges_redirect_and_final_bodies_to_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = search_scope().page_request(1)
    budget = scan_budget(bytes_remaining=13)
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(
            [
                StubResponse(
                    b"redirect",
                    status_code=302,
                    headers={"Location": request.url},
                    url=request.url,
                ),
                StubResponse(b"final", url=request.url),
            ],
        ),
    )

    content = fetch_reklama5_page(request, budget)

    assert content == b"final"
    assert budget.bytes_remaining == 13 - len(b"redirect") - len(b"final")


@pytest.mark.parametrize(
    "responses",
    [
        [
            StubResponse(
                b"redirect",
                status_code=302,
                headers={"Location": search_scope().page_request(1).url},
                url=search_scope().page_request(1).url,
            ),
            StubResponse(b"final"),
        ],
        [StubResponse(b"final")],
    ],
)
def test_reklama5_fetch_rejects_redirect_or_final_body_past_attempt_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[StubResponse],
) -> None:
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(responses),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(
            search_scope().page_request(1),
            scan_budget(bytes_remaining=4),
        )

    assert fetch_error.value.cause_type == "ScanByteLimitExceeded"
    assert not fetch_error.value.retryable


def test_reklama5_fetch_reads_and_charges_http_error_before_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = scan_budget(bytes_remaining=100)
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([StubResponse(b"unavailable", status_code=503)]),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), budget)

    assert fetch_error.value.cause_type == "HTTPError"
    assert fetch_error.value.retryable
    assert budget.bytes_remaining == 100 - len(b"unavailable")


def test_reklama5_fetch_prioritizes_oversized_http_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet(
            [
                StubResponse(
                    b"",
                    status_code=503,
                    chunks=(b"x" * MAX_REKLAMA5_RESPONSE_BYTES, b"y"),
                ),
            ],
        ),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.cause_type == "ResponseTooLarge"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize("status_code", range(400, 600))
def test_reklama5_fetch_classifies_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([StubResponse(b"error", status_code=status_code)]),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(search_scope().page_request(1), scan_budget())

    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is (
        status_code in {408, 429} or 500 <= status_code < 600
    )


def test_reklama5_fetch_rejects_empty_response_completed_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter((0.0, 2.0))
    monkeypatch.setattr(
        reklama5_http,
        "_create_session",
        lambda: RecordingGet([StubResponse(b"", chunks=())]),
    )
    monkeypatch.setattr(
        reklama5_http.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_reklama5_page(
            search_scope().page_request(1),
            scan_budget(bytes_remaining=100, expires_at=1),
        )

    assert fetch_error.value.cause_type == "ScanTimeLimitExceeded"
    assert not fetch_error.value.retryable


@pytest.mark.parametrize("remaining_seconds", [31.0, 0.25])
def test_reklama5_fetch_uses_positive_capped_request_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    remaining_seconds: float,
) -> None:
    get = RecordingGet([StubResponse(b"page")])
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10.0)

    fetch_reklama5_page(
        search_scope().page_request(1),
        scan_budget(expires_at=10.0 + remaining_seconds),
    )

    assert get.timeouts == [min(30.0, remaining_seconds)]
    assert get.timeouts[0] > 0


def test_reklama5_scan_budget_for_attempt_uses_fixed_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10.0)

    budget = Reklama5ScanBudget.for_attempt()

    assert budget.bytes_remaining == MAX_REKLAMA5_ATTEMPT_BYTES
    assert budget.redirects_remaining == 10
    assert budget.expires_at == 130.0
