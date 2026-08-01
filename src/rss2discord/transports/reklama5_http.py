from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin

import requests

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.reklama5_scope import (
    REKLAMA5_LABEL,
    Reklama5PageRequest,
)
from rss2discord.transports.reklama5_scope import (
    Reklama5SearchScope as _Reklama5SearchScope,
)

Reklama5SearchScope = _Reklama5SearchScope

REKLAMA5_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
MAX_REKLAMA5_RESPONSE_BYTES: Final = 2_097_152
MAX_REKLAMA5_ATTEMPT_BYTES: Final = 6_291_456
MAX_REKLAMA5_REDIRECTS: Final = 10
MAX_REKLAMA5_ATTEMPT_SECONDS: Final = 120.0
REKLAMA5_STREAM_CHUNK_BYTES: Final = 65_536

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


def fetch_reklama5_page(
    request: Reklama5PageRequest,
    budget: Reklama5ScanBudget,
) -> bytes:
    """Fetch one trusted search page within a caller-owned attempt budget."""
    current_url = request.url
    while True:
        response = _get_response(current_url, budget)
        try:
            with response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location")
                    if location is None:
                        raise _invalid_redirect()
                    target_url = urljoin(response.url, location)
                    if not request.scope.accepts_redirect(request, target_url):
                        raise _invalid_redirect()
                    budget.consume_redirect()
                    _read_content(response, budget)
                    current_url = target_url
                    continue
                content = _read_content(response, budget)
                if 400 <= response.status_code < 600:
                    raise _http_error(response)
                return content
        except FeedFetchError:
            raise
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.RequestException,
        ) as error:
            raise _transport_error(error, retryable=True) from None


def _get_response(
    url: str,
    budget: Reklama5ScanBudget,
) -> requests.Response:
    try:
        return requests.get(
            url,
            headers={
                "Accept": "text/html",
                "User-Agent": REKLAMA5_USER_AGENT,
            },
            timeout=budget.request_timeout(),
            allow_redirects=False,
            stream=True,
        )
    except FeedFetchError:
        raise
    except (requests.ConnectionError, requests.Timeout) as error:
        raise _transport_error(error, retryable=True) from None
    except requests.RequestException as error:
        raise _transport_error(error, retryable=False) from None


def _transport_error(
    error: requests.RequestException,
    *,
    retryable: bool,
) -> FeedFetchError:
    return FeedFetchError(
        REKLAMA5_LABEL,
        type(error).__name__,
        retryable=retryable,
    )


def _read_content(
    response: requests.Response,
    budget: Reklama5ScanBudget,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_REKLAMA5_RESPONSE_BYTES:
            raise FeedFetchError(REKLAMA5_LABEL, "ResponseTooLarge")

    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(
        chunk_size=REKLAMA5_STREAM_CHUNK_BYTES,
    )
    for chunk in chunks:
        if len(content) + len(chunk) > MAX_REKLAMA5_RESPONSE_BYTES:
            raise FeedFetchError(REKLAMA5_LABEL, "ResponseTooLarge")
        budget.consume_bytes(len(chunk))
        content.extend(chunk)
    budget.consume_bytes(0)
    return bytes(content)


def _invalid_redirect() -> FeedFetchError:
    return FeedFetchError(REKLAMA5_LABEL, "InvalidRedirect")


def _http_error(response: requests.Response) -> FeedFetchError:
    status_code = response.status_code
    return FeedFetchError(
        REKLAMA5_LABEL,
        "HTTPError",
        status_code=status_code,
        retryable=status_code in {408, 429} or 500 <= status_code < 600,
        retry_after=parse_retry_after(response.headers.get("Retry-After")),
    )
