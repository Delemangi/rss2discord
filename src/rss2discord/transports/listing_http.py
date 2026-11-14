"""Shared bounded HTML listing fetch path for provider transports."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urljoin

from curl_cffi import CurlECode, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.listing_session import (
    ListingHttpResponse,
    ListingHttpSession,
    create_listing_session,
)

_RETRYABLE_CURL_CODES: Final = frozenset(
    {
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


class ResponseHeaders(Protocol):
    """Minimal response capability for header-only helpers."""

    @property
    def headers(self) -> Mapping[str, str]: ...


class ListingBudget(Protocol):
    """Mutable byte, redirect, and deadline limits for one listing fetch."""

    bytes_remaining: int
    redirects_remaining: int
    expires_at: float

    def request_timeout(self) -> float: ...

    def consume_redirect(self) -> None: ...

    def consume_bytes(self, size: int) -> None: ...


@dataclass(frozen=True, slots=True)
class ListingHttpConfig:
    """Provider-specific labels and limits for the shared listing path."""

    label: str
    max_response_bytes: int
    user_agent: str
    challenge_cause: str


class BoundedListingContent:
    """Accumulate one response while retaining its exact local abort cause."""

    def __init__(self, budget: ListingBudget, config: ListingHttpConfig) -> None:
        self.content = bytearray()
        self.budget = budget
        self.max_bytes = config.max_response_bytes
        self.label = config.label
        self.abort_error: FeedFetchError | None = None

    def write(self, chunk: bytes) -> int:
        try:
            if len(self.content) + len(chunk) > self.max_bytes:
                self.abort_error = FeedFetchError(self.label, "ResponseTooLarge")
                return CURL_WRITEFUNC_ERROR
            self.budget.consume_bytes(len(chunk))
        except FeedFetchError as error:
            self.abort_error = error
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)


def fetch_listing_page(
    *,
    url: str,
    budget: ListingBudget,
    config: ListingHttpConfig,
    accepts_redirect: Callable[[str], bool],
    before_request: Callable[[], None] | None = None,
) -> bytes:
    """Fetch one trusted listing page within a caller-owned attempt budget."""
    session = create_listing_session()
    try:
        current_url = url
        while True:
            if before_request is not None:
                before_request()
            response, content = _get_response(session, current_url, budget, config)
            if is_cf_challenge(response):
                raise FeedFetchError(
                    config.label,
                    config.challenge_cause,
                    status_code=response.status_code,
                )
            if 300 <= response.status_code < 400:
                location = header_value(response.headers, "location")
                if location is None or "#" in location:
                    raise FeedFetchError(config.label, "InvalidRedirect")
                target_url = urljoin(response.url, location)
                if not accepts_redirect(target_url):
                    raise FeedFetchError(config.label, "InvalidRedirect")
                budget.consume_redirect()
                current_url = target_url
                continue
            if 400 <= response.status_code < 600:
                raise http_error(config.label, response)
            return content
    finally:
        session.close()


def _get_response(
    session: ListingHttpSession,
    url: str,
    budget: ListingBudget,
    config: ListingHttpConfig,
) -> tuple[ListingHttpResponse, bytes]:
    content = BoundedListingContent(budget, config)
    try:
        response = session.get(
            url,
            headers={"Accept": "text/html", "User-Agent": config.user_agent},
            allow_redirects=False,
            content_callback=content.write,
            timeout_ms=max(1, math.ceil(budget.request_timeout() * 1000)),
        )
        if content.abort_error is not None:
            raise content.abort_error
        budget.consume_bytes(0)
        validate_declared_content_length(
            response,
            config.max_response_bytes,
            config.label,
            invalid_as_zero=True,
        )
        return response, bytes(content.content)
    except FeedFetchError:
        raise
    except requests.RequestsError as error:
        if content.abort_error is not None:
            raise content.abort_error from None
        if time.monotonic() >= budget.expires_at:
            raise FeedFetchError(config.label, "ScanTimeLimitExceeded") from None
        raise transport_error(config.label, error) from None


def transport_error(label: str, error: requests.RequestsError) -> FeedFetchError:
    """Classify one transport failure, preserving the retryable curl codes."""
    cause_type = (
        "InvalidURL" if error.code == CurlECode.URL_MALFORMAT else type(error).__name__
    )
    return FeedFetchError(
        label,
        cause_type,
        retryable=error.code in _RETRYABLE_CURL_CODES,
    )


def validate_declared_content_length(
    response: ResponseHeaders,
    max_bytes: int,
    label: str,
    *,
    invalid_as_zero: bool,
) -> None:
    """Reject a declared body length above the per-response limit."""
    content_length = header_value(response.headers, "content-length")
    if content_length is None:
        return
    try:
        declared_bytes = int(content_length)
    except ValueError:
        if not invalid_as_zero:
            return
        declared_bytes = 0
    if declared_bytes > max_bytes:
        raise FeedFetchError(label, "ResponseTooLarge")


def http_error(label: str, response: ListingHttpResponse) -> FeedFetchError:
    """Build the shared retryable HTTP status failure."""
    status_code = response.status_code
    return FeedFetchError(
        label,
        "HTTPError",
        status_code=status_code,
        retryable=status_code in {408, 429} or 500 <= status_code < 600,
        retry_after=parse_retry_after(header_value(response.headers, "retry-after")),
    )


def is_cf_challenge(response: ListingHttpResponse) -> bool:
    """Report whether the response declares a Cloudflare challenge."""
    value = header_value(response.headers, "cf-mitigated")
    return value is not None and value.strip().casefold() == "challenge"


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """Return a header value using a case-insensitive name lookup."""
    folded_name = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == folded_name),
        None,
    )
