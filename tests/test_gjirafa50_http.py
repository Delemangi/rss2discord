import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.cookiejar import Cookie
from urllib.request import Request

import pytest
import requests

from rss2discord.transports import FeedFetchError, gjirafa50_http
from rss2discord.transports.gjirafa50_catalog import _OperationBudget
from rss2discord.transports.gjirafa50_http import (
    Gjirafa50HttpClient,
    Gjirafa50PageRequest,
    _read_content,
)
from rss2discord.transports.gjirafa50_models import Gjirafa50CatalogPage
from tests.gjirafa50_helpers import RecordingGet, StubResponse, catalog_payload


class SlowResponse(requests.Response):
    def iter_content(
        self,
        chunk_size: int | None = 1,
        decode_unicode: bool = False,
    ) -> Iterator[bytes]:
        del chunk_size, decode_unicode
        yield b"first"
        yield b"second"


class EmptyResponse(requests.Response):
    def iter_content(
        self,
        chunk_size: int | None = 1,
        decode_unicode: bool = False,
    ) -> Iterator[bytes]:
        del chunk_size, decode_unicode
        return iter(())


def test_http_rejects_unsafe_root_url() -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        Gjirafa50HttpClient().normalize_root_url("https://user@gjirafa50.mk:444/")


def test_response_stream_enforces_absolute_scan_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    budget.deadline = 1.0
    times = iter((0.0, 2.0))
    monkeypatch.setattr(gjirafa50_http.time, "monotonic", lambda: next(times))

    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        _read_content(SlowResponse(), budget=budget)


def test_http_budget_counts_every_redirect_request_and_response_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    redirect = StubResponse(b"redirect", status_code=302)
    redirect.headers["Location"] = "/product/search"
    payload = catalog_payload(0, ())
    get = RecordingGet([redirect, StubResponse(payload)])
    client = Gjirafa50HttpClient()
    monkeypatch.setattr(client._session, "get", get)
    monkeypatch.setattr(gjirafa50_http, "GJIRAFA50_REQUEST_INTERVAL_SECONDS", 0)

    fetched = client.fetch_page(
        "https://gjirafa50.mk/",
        Gjirafa50PageRequest(page=1, budget=budget),
        datetime.now(UTC),
    )

    assert fetched.response_bytes == len(b"redirect") + len(payload)
    assert budget.requests == 2
    assert budget.response_bytes == fetched.response_bytes + len(
        b"Location: /product/search\r\n",
    )


def test_http_budget_keeps_failed_response_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    get = RecordingGet([StubResponse(b"failure", status_code=500)])
    client = Gjirafa50HttpClient()
    monkeypatch.setattr(client._session, "get", get)
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
    client = Gjirafa50HttpClient()
    monkeypatch.setattr(client._session, "get", RecordingGet([StubResponse(payload)]))
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

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        _read_content(SlowResponse(), budget=budget)

    assert budget.response_bytes == len(b"firstsecond")


def test_response_headers_are_charged_to_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _OperationBudget(lambda: False)
    response = EmptyResponse()
    response.headers["X-Test"] = "abc"
    monkeypatch.setattr(gjirafa50_http, "GJIRAFA50_RESPONSE_BYTES", 5)

    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        _read_content(response, budget=budget)

    assert budget.response_bytes == len(b"X-Test: abc\r\n")


def test_http_session_rejects_response_cookies() -> None:
    client = Gjirafa50HttpClient()
    cookie = Cookie(
        version=0,
        name="untrusted",
        value="value",
        port=None,
        port_specified=False,
        domain="gjirafa50.mk",
        domain_specified=True,
        domain_initial_dot=False,
        path="/product/search",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )

    client._session.cookies.set_cookie_if_ok(
        cookie,
        Request("https://gjirafa50.mk/product/search"),
    )

    assert not client._session.cookies
