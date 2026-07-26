"""Bounded HTTP primitives for the Neksio catalog client."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, TypedDict
from urllib.parse import urljoin, urlsplit

import requests

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError

NEKSIO_LABEL: Final = "Neksio"
NEKSIO_HOST: Final = "g.store.neksio.mk"
NEKSIO_ORIGIN: Final = f"https://{NEKSIO_HOST}/"
NEKSIO_PAGE_SIZE: Final = 100
MAX_NEKSIO_RESPONSE_BYTES: Final = 1_048_576
NEKSIO_STREAM_CHUNK_BYTES: Final = 65_536
MAX_NEKSIO_REDIRECTS: Final = 10
MAX_NEKSIO_SCAN_RESPONSES: Final = 128
MAX_NEKSIO_SCAN_BYTES: Final = 33_554_432
MAX_NEKSIO_SCAN_SECONDS: Final = 300
NEKSIO_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
_HOMEPAGE_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {"Accept": "text/html", "User-Agent": NEKSIO_USER_AGENT},
)
_CATALOG_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": NEKSIO_USER_AGENT,
    },
)


class _PriceRange(TypedDict):
    minPrice: None
    maxPrice: None


class NeksioCatalogRequest(TypedDict):
    categoryId: int
    manufacturerIds: list[int]
    subCategoryIds: list[int]
    page: int
    pageSize: int
    description: None
    selectedMinMaxPrice: _PriceRange
    orderBy: int
    quantityStock: int


type _RequestSender = Callable[[str, float], AbstractContextManager[requests.Response]]


@dataclass(slots=True)
class NeksioScanBudget:
    """Mutable response, byte, and elapsed-time budget shared by one catalog scan."""

    responses_remaining: int
    bytes_remaining: int
    expires_at: float

    @classmethod
    def for_catalog_scan(cls) -> NeksioScanBudget:
        return cls(
            responses_remaining=MAX_NEKSIO_SCAN_RESPONSES,
            bytes_remaining=MAX_NEKSIO_SCAN_BYTES,
            expires_at=time.monotonic() + MAX_NEKSIO_SCAN_SECONDS,
        )

    def consume_response(self) -> float:
        remaining_seconds = self.expires_at - time.monotonic()
        if remaining_seconds <= 0:
            raise FeedFetchError(NEKSIO_LABEL, "ScanTimeLimitExceeded")
        if self.responses_remaining <= 0:
            raise FeedFetchError(NEKSIO_LABEL, "ScanResponseLimitExceeded")
        self.responses_remaining -= 1
        return min(30.0, remaining_seconds)

    def consume_bytes(self, size: int) -> None:
        if time.monotonic() >= self.expires_at:
            raise FeedFetchError(NEKSIO_LABEL, "ScanTimeLimitExceeded")
        if size > self.bytes_remaining:
            raise FeedFetchError(NEKSIO_LABEL, "ScanByteLimitExceeded")
        self.bytes_remaining -= size


def origin_url(url: str) -> str:
    """Return a credential-free source origin with a root path."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        port = parsed.port
    except ValueError:
        raise FeedFetchError(NEKSIO_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or hostname != NEKSIO_HOST
        or username is not None
        or password is not None
        or port not in {None, 443}
    ):
        raise FeedFetchError(NEKSIO_LABEL, "InvalidUrl")
    return NEKSIO_ORIGIN


def fetch_homepage(origin: str, *, budget: NeksioScanBudget | None = None) -> bytes:
    """Fetch the capped source homepage without automatic redirects."""
    return _fetch_content(
        origin,
        lambda current_url, timeout: requests.get(
            current_url,
            headers=_HOMEPAGE_HEADERS,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
        ),
        budget,
    )


def fetch_page_content(
    endpoint: str,
    body: NeksioCatalogRequest,
    *,
    budget: NeksioScanBudget | None = None,
) -> bytes:
    """Post one catalog page request and return its capped response bytes."""
    return _fetch_content(
        endpoint,
        lambda current_url, timeout: requests.post(
            current_url,
            headers=_CATALOG_HEADERS,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
            json=body,
        ),
        budget,
    )


def _fetch_content(
    url: str,
    send: _RequestSender,
    budget: NeksioScanBudget | None,
) -> bytes:
    try:
        current_url = url
        for _ in range(MAX_NEKSIO_REDIRECTS + 1):
            timeout = budget.consume_response() if budget is not None else 30.0
            with send(current_url, timeout) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location")
                    if location is None:
                        raise FeedFetchError(NEKSIO_LABEL, "InvalidRedirect")
                    _read_content(response, budget)
                    current_url = _same_origin_redirect_url(current_url, location)
                    continue
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    status_code = response.status_code
                    raise FeedFetchError(
                        NEKSIO_LABEL,
                        "HTTPError",
                        status_code=status_code,
                        retryable=(
                            status_code in {408, 429} or 500 <= status_code < 600
                        ),
                        retry_after=parse_retry_after(
                            response.headers.get("Retry-After"),
                        ),
                    ) from None
                return _read_content(response, budget)
        raise FeedFetchError(NEKSIO_LABEL, "TooManyRedirects")
    except ValueError:
        raise FeedFetchError(NEKSIO_LABEL, "InvalidUrl") from None
    except (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
    ) as error:
        raise FeedFetchError(
            NEKSIO_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None
    except requests.RequestException as error:
        raise FeedFetchError(NEKSIO_LABEL, type(error).__name__) from None


def _same_origin_redirect_url(current_url: str, location: str) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        current = urlsplit(current_url)
        redirected = urlsplit(redirected_url)
        default_port = 443 if current.scheme == "https" else 80
        current_port = current.port or default_port
        redirected_port = redirected.port or default_port
    except ValueError:
        raise FeedFetchError(NEKSIO_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != current.scheme
        or redirected.hostname != current.hostname
        or redirected_port != current_port
        or redirected.username is not None
        or redirected.password is not None
    ):
        raise FeedFetchError(NEKSIO_LABEL, "InvalidRedirect")
    return redirected_url


def _read_content(
    response: requests.Response,
    budget: NeksioScanBudget | None,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_NEKSIO_RESPONSE_BYTES:
            raise FeedFetchError(NEKSIO_LABEL, "ResponseTooLarge")
    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(
        chunk_size=NEKSIO_STREAM_CHUNK_BYTES,
    )
    for chunk in chunks:
        if len(content) + len(chunk) > MAX_NEKSIO_RESPONSE_BYTES:
            raise FeedFetchError(NEKSIO_LABEL, "ResponseTooLarge")
        if budget is not None:
            budget.consume_bytes(len(chunk))
        content.extend(chunk)
    if budget is not None:
        budget.consume_bytes(0)
    return bytes(content)
