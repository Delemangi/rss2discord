import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from curl_cffi import CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import FeedFetchError, gjirafa50_http, gjirafa50_session
from rss2discord.transports.gjirafa50_catalog import _OperationBudget
from rss2discord.transports.gjirafa50_http import (
    Gjirafa50HttpClient,
    Gjirafa50PageRequest,
    _BoundedContent,
)
from rss2discord.transports.gjirafa50_models import Gjirafa50CatalogPage
from tests.gjirafa50_helpers import RecordingGet, StubResponse, catalog_payload


def test_http_rejects_unsafe_root_url() -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        Gjirafa50HttpClient().normalize_root_url("https://user@gjirafa50.mk:444/")


def test_response_stream_enforces_absolute_scan_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    budget.deadline = 1.0
    monkeypatch.setattr(gjirafa50_http.time, "monotonic", lambda: 2.0)
    content = _BoundedContent(budget)

    assert content.write(b"chunk") == CURL_WRITEFUNC_ERROR
    assert isinstance(content.abort_error, FeedFetchError)
    assert "ScanTimeLimitExceeded" in str(content.abort_error)


def test_http_budget_counts_every_redirect_request_and_response_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    redirect = StubResponse(b"redirect", status_code=302)
    redirect.headers["Location"] = "/product/search"
    payload = catalog_payload(0, ())
    get = RecordingGet([redirect, StubResponse(payload)])
    client = Gjirafa50HttpClient(get)
    monkeypatch.setattr(gjirafa50_http, "GJIRAFA50_REQUEST_INTERVAL_SECONDS", 0)

    fetched = client.fetch_page(
        "https://gjirafa50.mk/",
        Gjirafa50PageRequest(page=1, budget=budget),
        datetime.now(UTC),
    )

    assert fetched.response_bytes == len(b"redirect") + len(payload) + len(
        b"Location: /product/search\r\n",
    )
    assert budget.requests == 2
    assert budget.response_bytes == fetched.response_bytes


def test_http_budget_keeps_failed_response_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    get = RecordingGet([StubResponse(b"failure", status_code=500)])
    client = Gjirafa50HttpClient(get)
    monkeypatch.setattr(gjirafa50_http, "GJIRAFA50_REQUEST_INTERVAL_SECONDS", 0)

    with pytest.raises(FeedFetchError, match="HTTP 500"):
        client.fetch_page(
            "https://gjirafa50.mk/",
            Gjirafa50PageRequest(page=1, budget=budget),
            datetime.now(UTC),
        )

    assert budget.requests == 1
    assert budget.response_bytes == len(b"failure")


def test_http_enforces_deadline_after_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    payload = catalog_payload(0, ())
    client = Gjirafa50HttpClient(RecordingGet([StubResponse(payload)]))
    monkeypatch.setattr(gjirafa50_http, "GJIRAFA50_REQUEST_INTERVAL_SECONDS", 0)
    parse = gjirafa50_http.parse_gjirafa50_page

    def parse_after_deadline(
        content: bytes,
        observed_at: datetime,
    ) -> Gjirafa50CatalogPage:
        page = parse(content, observed_at)
        budget.deadline = time.monotonic()
        return page

    monkeypatch.setattr(gjirafa50_http, "parse_gjirafa50_page", parse_after_deadline)

    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        client.fetch_page(
            "https://gjirafa50.mk/",
            Gjirafa50PageRequest(page=1, budget=budget),
            datetime.now(UTC),
        )


def test_response_stream_charges_chunk_that_crosses_response_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    monkeypatch.setattr(gjirafa50_http, "GJIRAFA50_RESPONSE_BYTES", 5)
    content = _BoundedContent(budget)

    assert content.write(b"first") == 5
    assert content.write(b"second") == CURL_WRITEFUNC_ERROR

    assert budget.response_bytes == len(b"firstsecond")
    assert isinstance(content.abort_error, FeedFetchError)
    assert "ResponseTooLarge" in str(content.abort_error)


def test_response_headers_are_charged_to_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    response = StubResponse(b"")
    response.headers["X-Test"] = "abc"
    monkeypatch.setattr(gjirafa50_http, "GJIRAFA50_RESPONSE_BYTES", 5)
    client = Gjirafa50HttpClient(RecordingGet([response]))

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        client._request({}, root_url="https://gjirafa50.mk/", budget=budget)

    assert budget.response_bytes == len(b"X-Test: abc\r\n")


def test_http_session_enforces_total_timeout_and_environment_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_options: list[Mapping[CurlOpt, int | str]] = []
    captured_session_options: list[tuple[bool, bool, bool]] = []

    @dataclass(frozen=True, slots=True)
    class SessionResponse:
        status_code: int = 200
        headers: Mapping[str, str] = field(default_factory=dict)

    class SessionStub:
        def get(
            self,
            url: str,
            *,
            params: Mapping[str, str | int],
            headers: Mapping[str, str],
            allow_redirects: bool,
            content_callback: Callable[[bytes], int],
            impersonate: str,
        ) -> SessionResponse:
            del url, params, headers, allow_redirects, impersonate
            content_callback(b"payload")
            return SessionResponse()

        def close(self) -> None:
            return

    def create_session(
        *,
        trust_env: bool,
        discard_cookies: bool,
        default_headers: bool,
        curl_options: Mapping[CurlOpt, int | str],
    ) -> SessionStub:
        captured_session_options.append(
            (trust_env, discard_cookies, default_headers),
        )
        captured_options.append(curl_options)
        return SessionStub()

    monkeypatch.setattr(gjirafa50_session.requests, "Session", create_session)
    content = bytearray()

    def write_content(chunk: bytes) -> int:
        content.extend(chunk)
        return len(chunk)

    response = gjirafa50_session.create_gjirafa50_session().get(
        "https://gjirafa50.mk/product/search",
        params={"pagenumber": 1},
        headers={"Accept": "application/json"},
        allow_redirects=False,
        content_callback=write_content,
        timeout_ms=1234,
    )

    assert response.status_code == 200
    assert content == b"payload"
    assert captured_session_options == [(False, True, False)]
    assert captured_options[0][CurlOpt.TIMEOUT_MS] == 1234
    assert captured_options[0][CurlOpt.PROXY] == ""
    assert captured_options[0][CurlOpt.NETRC] == 0


def test_response_stream_stops_when_shutdown_is_requested() -> None:
    budget = _OperationBudget(lambda: True)
    content = _BoundedContent(budget)

    assert content.write(b"chunk") == CURL_WRITEFUNC_ERROR
    assert isinstance(content.abort_error, FeedFetchInterruptedError)
