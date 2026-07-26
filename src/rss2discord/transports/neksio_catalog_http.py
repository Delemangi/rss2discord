"""Bounded HTTP primitives for the Neksio catalog client."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Final, TypedDict
from urllib.parse import urljoin, urlsplit

import requests

from rss2discord.transports.base import FeedFetchError

NEKSIO_LABEL: Final = "Neksio"
NEKSIO_HOST: Final = "g.store.neksio.mk"
NEKSIO_ORIGIN: Final = f"https://{NEKSIO_HOST}/"
NEKSIO_PAGE_SIZE: Final = 100
MAX_NEKSIO_RESPONSE_BYTES: Final = 1_048_576
NEKSIO_STREAM_CHUNK_BYTES: Final = 65_536
MAX_NEKSIO_REDIRECTS: Final = 10
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


type _RequestSender = Callable[[str], AbstractContextManager[requests.Response]]


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


def fetch_homepage(origin: str) -> bytes:
    """Fetch the capped source homepage without automatic redirects."""
    return _fetch_content(
        origin,
        lambda current_url: requests.get(
            current_url,
            headers=_HOMEPAGE_HEADERS,
            timeout=30,
            stream=True,
            allow_redirects=False,
        ),
    )


def fetch_page_content(endpoint: str, body: NeksioCatalogRequest) -> bytes:
    """Post one catalog page request and return its capped response bytes."""
    return _fetch_content(
        endpoint,
        lambda current_url: requests.post(
            current_url,
            headers=_CATALOG_HEADERS,
            timeout=30,
            stream=True,
            allow_redirects=False,
            json=body,
        ),
    )


def _fetch_content(url: str, send: _RequestSender) -> bytes:
    try:
        current_url = url
        for _ in range(MAX_NEKSIO_REDIRECTS + 1):
            with send(current_url) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location")
                    if location is None:
                        raise FeedFetchError(NEKSIO_LABEL, "InvalidRedirect")
                    _read_content(response)
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
                        retry_after=_parse_retry_after(response.headers.get("Retry-After")),
                    ) from None
                return _read_content(response)
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


def _read_content(response: requests.Response) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_NEKSIO_RESPONSE_BYTES:
            raise FeedFetchError(NEKSIO_LABEL, "ResponseTooLarge")
    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(chunk_size=NEKSIO_STREAM_CHUNK_BYTES)
    for chunk in chunks:
        if len(content) + len(chunk) > MAX_NEKSIO_RESPONSE_BYTES:
            raise FeedFetchError(NEKSIO_LABEL, "ResponseTooLarge")
        content.extend(chunk)
    return bytes(content)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)
    return retry_after if math.isfinite(retry_after) and retry_after >= 0 else None
