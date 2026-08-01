from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

from curl_cffi import CurlECode, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.reklama5_scope import (
    REKLAMA5_LABEL,
    Reklama5PageRequest,
)
from rss2discord.transports.reklama5_scope import (
    Reklama5SearchScope as _Reklama5SearchScope,
)
from rss2discord.transports.reklama5_session import (
    Reklama5HttpResponse,
    Reklama5HttpSession,
    create_reklama5_session,
)

Reklama5SearchScope = _Reklama5SearchScope

REKLAMA5_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
MAX_REKLAMA5_RESPONSE_BYTES: Final = 2_097_152
MAX_REKLAMA5_ATTEMPT_BYTES: Final = 6_291_456
MAX_REKLAMA5_REDIRECTS: Final = 10
MAX_REKLAMA5_ATTEMPT_SECONDS: Final = 120.0

@dataclass(slots=True)
class Reklama5ScanBudget:
    """Track mutable byte, redirect, and deadline limits for one fetch attempt."""

    bytes_remaining: int
    redirects_remaining: int
    expires_at: float

    @classmethod
    def for_attempt(cls) -> Reklama5ScanBudget:
        return cls(
            bytes_remaining=MAX_REKLAMA5_ATTEMPT_BYTES,
            redirects_remaining=MAX_REKLAMA5_REDIRECTS,
            expires_at=time.monotonic() + MAX_REKLAMA5_ATTEMPT_SECONDS,
        )

    def request_timeout(self) -> float:
        remaining_seconds = self.expires_at - time.monotonic()
        if remaining_seconds <= 0:
            raise FeedFetchError(REKLAMA5_LABEL, "ScanTimeLimitExceeded")
        return min(30.0, remaining_seconds)

    def consume_redirect(self) -> None:
        if self.redirects_remaining <= 0:
            raise FeedFetchError(REKLAMA5_LABEL, "TooManyRedirects")
        self.redirects_remaining -= 1

    def consume_bytes(self, size: int) -> None:
        if time.monotonic() >= self.expires_at:
            raise FeedFetchError(REKLAMA5_LABEL, "ScanTimeLimitExceeded")
        if size > self.bytes_remaining:
            raise FeedFetchError(REKLAMA5_LABEL, "ScanByteLimitExceeded")
        self.bytes_remaining -= size


class _BoundedContent:
    """Accumulate one response while recording the exact local abort cause."""

    def __init__(self, budget: Reklama5ScanBudget) -> None:
        self.content = bytearray()
        self.budget = budget
        self.abort_error: FeedFetchError | None = None

    def write(self, chunk: bytes) -> int:
        try:
            if len(self.content) + len(chunk) > MAX_REKLAMA5_RESPONSE_BYTES:
                self.abort_error = FeedFetchError(REKLAMA5_LABEL, "ResponseTooLarge")
                return CURL_WRITEFUNC_ERROR
            self.budget.consume_bytes(len(chunk))
        except FeedFetchError as error:
            self.abort_error = error
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)


def _create_session() -> Reklama5HttpSession:
    return create_reklama5_session()


def fetch_reklama5_page(
    request: Reklama5PageRequest,
    budget: Reklama5ScanBudget,
) -> bytes:
    """Fetch one trusted search page within a caller-owned attempt budget."""
    session = _create_session()
    try:
        current_url = request.url
        while True:
            response, content = _get_response(session, current_url, budget)
            if 300 <= response.status_code < 400:
                location = _header(response.headers, "location")
                if location is None or "#" in location:
                    raise _invalid_redirect()
                target_url = urljoin(response.url, location)
                if not request.scope.accepts_redirect(request, target_url):
                    raise _invalid_redirect()
                budget.consume_redirect()
                current_url = target_url
                continue
            if 400 <= response.status_code < 600:
                raise _http_error(response)
            return content
    finally:
        session.close()


def _get_response(
    session: Reklama5HttpSession,
    url: str,
    budget: Reklama5ScanBudget,
) -> tuple[Reklama5HttpResponse, bytes]:
    content = _BoundedContent(budget)
    try:
        response = session.get(
            url,
            headers={
                "Accept": "text/html",
                "User-Agent": REKLAMA5_USER_AGENT,
            },
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
            raise FeedFetchError(REKLAMA5_LABEL, "ScanTimeLimitExceeded") from None
        raise _transport_error(error) from None


def _transport_error(
    error: requests.RequestsError,
) -> FeedFetchError:
    cause_type = "InvalidURL" if error.code == CurlECode.URL_MALFORMAT else type(error).__name__
    return FeedFetchError(
        REKLAMA5_LABEL,
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
        },
    )


def _validate_declared_content_length(response: Reklama5HttpResponse) -> None:
    content_length = _header(response.headers, "content-length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_REKLAMA5_RESPONSE_BYTES:
            raise FeedFetchError(REKLAMA5_LABEL, "ResponseTooLarge")

def _invalid_redirect() -> FeedFetchError:
    return FeedFetchError(REKLAMA5_LABEL, "InvalidRedirect")


def _http_error(response: Reklama5HttpResponse) -> FeedFetchError:
    status_code = response.status_code
    return FeedFetchError(
        REKLAMA5_LABEL,
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
