from collections.abc import Callable, Mapping

import certifi
import pytest
from curl_cffi import CurlOpt
from pydantic import JsonValue

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import ddstore_http
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.ddstore_budget import DDStoreScanBudget, DDStoreScanLimits
from rss2discord.transports.ddstore_catalog import DDStoreCatalogClient
from rss2discord.transports.ddstore_http import (
    CatalogPageRequest,
    DDStoreHttpClient,
)
from tests.ddstore_helpers import (
    CATALOG_URL,
    RecordingPost,
    StubResponse,
    catalog_payload,
    product_payload,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_budget(
    clock: FakeClock,
    shutdown: list[bool] | None = None,
) -> DDStoreScanBudget:
    return DDStoreScanBudget(
        limits=DDStoreScanLimits(seconds=5.0, response_bytes=2_097_152),
        monotonic=clock.monotonic,
        is_shutdown_requested=(lambda: shutdown[0])
        if shutdown is not None
        else lambda: False,
    )


def test_ddstore_http_clamps_request_timeout_to_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    clock = FakeClock()
    post = RecordingPost(
        [StubResponse(catalog_payload(1, [product_payload("1")], current_page=1))],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When
    DDStoreHttpClient().fetch_page(
        "https://ddstore.mk/graphql",
        CatalogPageRequest(current_page=1, max_single_response_bytes=2_097_152),
        make_budget(clock),
    )

    # Then
    assert post.curl_options == [{CurlOpt.TIMEOUT_MS: 5_000}]


def test_ddstore_http_caps_each_transfer_below_shutdown_grace_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    clock = FakeClock()
    post = RecordingPost(
        [StubResponse(catalog_payload(1, [product_payload("1")], current_page=1))],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)
    budget = DDStoreScanBudget(
        limits=DDStoreScanLimits(seconds=300.0, response_bytes=2_097_152),
        monotonic=clock.monotonic,
        is_shutdown_requested=lambda: False,
    )

    # When
    DDStoreHttpClient().fetch_page(
        "https://ddstore.mk/graphql",
        CatalogPageRequest(current_page=1, max_single_response_bytes=2_097_152),
        budget,
    )

    # Then
    assert post.curl_options == [{CurlOpt.TIMEOUT_MS: 10_000}]


def test_ddstore_http_rejects_chunk_received_after_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    clock = FakeClock()
    content = catalog_payload(1, [product_payload("1")], current_page=1)
    post = RecordingPost(
        [StubResponse(content, chunks=(content,), on_chunk=lambda: clock.advance(6))],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanTimeout"):
        DDStoreHttpClient().fetch_page(
            "https://ddstore.mk/graphql",
            CatalogPageRequest(current_page=1, max_single_response_bytes=2_097_152),
            make_budget(clock),
        )


def test_ddstore_http_stops_streaming_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    clock = FakeClock()
    shutdown = [False]
    content = catalog_payload(1, [product_payload("1")], current_page=1)
    post = RecordingPost(
        [
            StubResponse(
                content,
                chunks=(content,),
                on_chunk=lambda: shutdown.__setitem__(0, True),
            ),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        DDStoreHttpClient().fetch_page(
            "https://ddstore.mk/graphql",
            CatalogPageRequest(current_page=1, max_single_response_bytes=2_097_152),
            make_budget(clock, shutdown),
        )


def test_ddstore_catalog_retry_reuses_one_absolute_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    clock = FakeClock()
    first_page = catalog_payload(
        501,
        [product_payload(str(index)) for index in range(1, 501)],
        current_page=1,
    )
    final_page = catalog_payload(501, [product_payload("501")], current_page=2)
    post = RecordingPost(
        [
            StubResponse(
                first_page,
                chunks=(first_page,),
                on_chunk=lambda: clock.advance(295),
            ),
            StubResponse(b"retry", status_code=503),
            StubResponse(first_page),
            StubResponse(final_page),
        ],
    )
    monkeypatch.setattr(ddstore_http, "_create_session", lambda: post)
    retry_policy = FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )

    # When
    products = DDStoreCatalogClient(monotonic_clock=clock.monotonic).fetch_catalog(
        CATALOG_URL,
        retry_policy=retry_policy,
        is_shutdown_requested=lambda: False,
    )

    # Then
    assert len(products) == 501
    assert post.curl_options == [
        {CurlOpt.TIMEOUT_MS: 10_000},
        {CurlOpt.TIMEOUT_MS: 5_000},
        {CurlOpt.TIMEOUT_MS: 5_000},
        {CurlOpt.TIMEOUT_MS: 5_000},
    ]


def test_ddstore_catalog_retry_aborts_when_delay_reaches_remaining_budget() -> None:
    # Given
    clock = FakeClock()
    budget = make_budget(clock)
    clock.advance(3)
    sleeps: list[float] = []
    attempts = 0

    def record_sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        return True

    retry_policy = FetchRetryPolicy(
        sleep=record_sleep,
        on_retry=lambda error, delay: None,
    )

    def fail_once() -> None:
        nonlocal attempts
        attempts += 1
        raise FeedFetchError(
            "DDStore",
            "HTTPError",
            retryable=True,
            retry_after=2,
        )

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanTimeout"):
        retry_policy.execute(fail_once, retry_guard=budget.require_retry_delay)
    assert attempts == 1
    assert sleeps == []


def test_ddstore_http_creates_dedicated_stateless_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    clock = FakeClock()
    response_content = catalog_payload(1, [product_payload("1")], current_page=1)
    captured_trust_env: list[bool] = []
    captured_discard_cookies: list[bool] = []
    captured_impersonation: list[str] = []
    captured_default_headers: list[bool] = []
    captured_curl_options: list[Mapping[CurlOpt, int | str]] = []
    closed = [False]

    class SessionStub:
        def post(
            self,
            url: str,
            *,
            json: dict[str, JsonValue] | list[JsonValue] | None,
            headers: Mapping[str, str],
            allow_redirects: bool,
            content_callback: Callable[[bytes], int],
        ) -> StubResponse:
            del url, json, headers, allow_redirects
            content_callback(response_content)
            return StubResponse(response_content)

        def close(self) -> None:
            closed[0] = True

    def create_session(
        *,
        trust_env: bool,
        discard_cookies: bool,
        impersonate: str,
        default_headers: bool,
        curl_options: Mapping[CurlOpt, int | str],
    ) -> SessionStub:
        captured_trust_env.append(trust_env)
        captured_discard_cookies.append(discard_cookies)
        captured_impersonation.append(impersonate)
        captured_default_headers.append(default_headers)
        captured_curl_options.append(curl_options)
        return SessionStub()

    monkeypatch.setattr(ddstore_http.requests, "Session", create_session)

    # When
    DDStoreHttpClient().fetch_page(
        "https://ddstore.mk/graphql",
        CatalogPageRequest(current_page=1, max_single_response_bytes=2_097_152),
        make_budget(clock),
    )

    # Then
    assert captured_trust_env == [False]
    assert captured_discard_cookies == [True]
    assert captured_impersonation == ["chrome"]
    assert captured_default_headers == [False]
    assert captured_curl_options == [
        {
            CurlOpt.TIMEOUT_MS: 5_000,
            CurlOpt.PROXY: "",
            CurlOpt.NETRC: 0,
            CurlOpt.CAINFO: certifi.where(),
        },
    ]
    assert closed == [True]
