from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

from curl_cffi import CurlECode, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.discord.client import SleepCallback
from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer
from rss2discord.transports.pazar3_scope import PAZAR3_LABEL, Pazar3PageRequest
from rss2discord.transports.pazar3_session import (
    Pazar3HttpResponse,
    Pazar3HttpSession,
    create_pazar3_session,
)

MAX_PAZAR3_RESPONSE_BYTES: Final = 2_097_152
MAX_PAZAR3_ATTEMPT_BYTES: Final = 6_291_456
MAX_PAZAR3_REDIRECTS: Final = 10
MAX_PAZAR3_ATTEMPT_SECONDS: Final = 120.0
MAX_PAZAR3_CATALOG_ATTEMPT_BYTES: Final = 20 * 1024 * 1024
MAX_PAZAR3_CATALOG_ATTEMPT_SECONDS: Final = 300.0
PAZAR3_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"


@dataclass(slots=True)  # noqa: RUF100  # noqa: MUTABLE_OK
class Pazar3ScanBudget:
    """Track mutable byte, redirect, and deadline limits for one fetch attempt."""

    bytes_remaining: int
    redirects_remaining: int
    expires_at: float

    @classmethod
    def for_attempt(cls) -> Pazar3ScanBudget:
        return cls(
            MAX_PAZAR3_ATTEMPT_BYTES,
            MAX_PAZAR3_REDIRECTS,
            time.monotonic() + MAX_PAZAR3_ATTEMPT_SECONDS,
        )

    @classmethod
    def for_catalog(cls) -> Pazar3ScanBudget:
        return cls(
            MAX_PAZAR3_CATALOG_ATTEMPT_BYTES,
            MAX_PAZAR3_REDIRECTS,
            time.monotonic() + MAX_PAZAR3_CATALOG_ATTEMPT_SECONDS,
        )

    def request_timeout(self) -> float:
        remaining_seconds = self.expires_at - time.monotonic()
        if remaining_seconds <= 0:
            raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded")
        return min(30.0, remaining_seconds)

    def consume_redirect(self) -> None:
        if self.redirects_remaining <= 0:
            raise FeedFetchError(PAZAR3_LABEL, "TooManyRedirects")
        self.redirects_remaining -= 1

    def consume_bytes(self, size: int) -> None:
        if time.monotonic() >= self.expires_at:
            raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded")
        if size > self.bytes_remaining:
            raise FeedFetchError(PAZAR3_LABEL, "ScanByteLimitExceeded")
        self.bytes_remaining -= size


class _BoundedContent:
    """Accumulate one response while retaining its exact local abort cause."""

    def __init__(self, budget: Pazar3ScanBudget) -> None:
        self.content = bytearray()
        self.budget = budget
        self.abort_error: FeedFetchError | None = None

    def write(self, chunk: bytes) -> int:
        try:
            if len(self.content) + len(chunk) > MAX_PAZAR3_RESPONSE_BYTES:
                self.abort_error = FeedFetchError(PAZAR3_LABEL, "ResponseTooLarge")
                return CURL_WRITEFUNC_ERROR
            self.budget.consume_bytes(len(chunk))
        except FeedFetchError as error:
            self.abort_error = error
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)


def _create_session() -> Pazar3HttpSession:
    return create_pazar3_session()


def fetch_pazar3_page(
    request: Pazar3PageRequest,
    budget: Pazar3ScanBudget,
    pacer: Pazar3RequestPacer,
    sleep: SleepCallback,
    is_shutdown_requested: Callable[[], bool],
) -> bytes:
    session = _create_session()
    try:
        current_url = request.url
        while True:
            pacer.wait(sleep, is_shutdown_requested, budget.expires_at)
            response, content = _get_response(session, current_url, budget)
            if 300 <= response.status_code < 400:
                location = _header(response.headers, "location")
                if location is None or "#" in location:
                    raise FeedFetchError(PAZAR3_LABEL, "InvalidRedirect")
                target_url = urljoin(response.url, location)
                if not request.scope.accepts_redirect(request, target_url):
                    raise FeedFetchError(PAZAR3_LABEL, "InvalidRedirect")
                budget.consume_redirect()
                current_url = target_url
                continue
            if 400 <= response.status_code < 600:
                raise _http_error(response)
            return content
    finally:
        session.close()


def _get_response(
    session: Pazar3HttpSession,
    url: str,
    budget: Pazar3ScanBudget,
) -> tuple[Pazar3HttpResponse, bytes]:
    content = _BoundedContent(budget)
    try:
        response = session.get(
            url,
            headers={"Accept": "text/html", "User-Agent": PAZAR3_USER_AGENT},
            allow_redirects=False,
            content_callback=content.write,
            timeout_ms=max(1, math.ceil(budget.request_timeout() * 1000)),
        )
        if content.abort_error is not None:
            raise content.abort_error
        budget.consume_bytes(0)
        _validate_declared_content_length(response)
        return response, bytes(content.content)
    except FeedFetchError:
        raise
    except requests.RequestsError as error:
        if content.abort_error is not None:
            raise content.abort_error from None
        if time.monotonic() >= budget.expires_at:
            raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded") from None
        raise _transport_error(error) from None


def _transport_error(error: requests.RequestsError) -> FeedFetchError:
    cause_type = (
        "InvalidURL" if error.code == CurlECode.URL_MALFORMAT else type(error).__name__
    )
    return FeedFetchError(
        PAZAR3_LABEL,
        cause_type,
        retryable=error.code
        in {
            CurlECode.COULDNT_RESOLVE_PROXY,
            CurlECode.COULDNT_RESOLVE_HOST,
            CurlECode.COULDNT_CONNECT,
            CurlECode.OPERATION_TIMEDOUT,
            CurlECode.GOT_NOTHING,
            CurlECode.SEND_ERROR,
            CurlECode.RECV_ERROR,
            CurlECode.PARTIAL_FILE,
            CurlECode.HTTP2,
            CurlECode.HTTP2_STREAM,
            CurlECode.HTTP3,
            CurlECode.QUIC_CONNECT_ERROR,
        },
    )


def _validate_declared_content_length(response: Pazar3HttpResponse) -> None:
    content_length = _header(response.headers, "content-length")
    if content_length is None:
        return
    try:
        declared_bytes = int(content_length)
    except ValueError:
        declared_bytes = 0
    if declared_bytes > MAX_PAZAR3_RESPONSE_BYTES:
        raise FeedFetchError(PAZAR3_LABEL, "ResponseTooLarge")


def _http_error(response: Pazar3HttpResponse) -> FeedFetchError:
    status_code = response.status_code
    return FeedFetchError(
        PAZAR3_LABEL,
        "HTTPError",
        status_code=status_code,
        retryable=status_code in {408, 429} or 500 <= status_code < 600,
        retry_after=parse_retry_after(_header(response.headers, "retry-after")),
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded_name = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == folded_name),
        None,
    )
