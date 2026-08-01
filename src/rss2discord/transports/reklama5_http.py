from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final
from urllib.parse import (
    SplitResult,
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import requests

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError

REKLAMA5_LABEL: Final = "Reklama5"
REKLAMA5_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
MAX_REKLAMA5_RESPONSE_BYTES: Final = 2_097_152
MAX_REKLAMA5_ATTEMPT_BYTES: Final = 6_291_456
MAX_REKLAMA5_REDIRECTS: Final = 10
MAX_REKLAMA5_ATTEMPT_SECONDS: Final = 120.0
REKLAMA5_STREAM_CHUNK_BYTES: Final = 65_536

_ALLOWED_HOSTS: Final = frozenset({"reklama5.mk", "www.reklama5.mk"})
_ALLOWED_PATHS: Final = frozenset(
    {"/Search", "/Search/", "/Search/Index", "/Search/Index/"},
)
_OWNED_QUERY_KEYS: Final = frozenset({"sortbyprice", "pageview", "page"})
_CANONICAL_QUERY_KEYS: Final = (
    ("sortbyprice", "SortByPrice"),
    ("pageview", "pageView"),
    ("page", "page"),
)

type _ParsedSearchUrl = tuple[SplitResult, str, int]


@dataclass(frozen=True, slots=True)
class Reklama5SearchScope:
    scheme: str
    host: str
    port: int
    configured_path: str
    caller_query: tuple[tuple[str, str], ...]

    @classmethod
    def from_url(cls, url: str) -> Reklama5SearchScope:
        trusted_url = _parse_search_url(url)
        if trusted_url is None:
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidUrl")
        parsed, host, port = trusted_url
        caller_query = tuple(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _OWNED_QUERY_KEYS
        )
        return cls(
            scheme=parsed.scheme,
            host=host,
            port=port,
            configured_path=parsed.path,
            caller_query=caller_query,
        )

    def page_request(self, page: int) -> Reklama5PageRequest:
        if page not in range(1, 4):
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")
        query = (
            *self.caller_query,
            ("SortByPrice", "2"),
            ("pageView", "1"),
            ("page", str(page)),
        )
        url = urlunsplit(
            (
                self.scheme,
                self.host,
                self.configured_path,
                urlencode(query, doseq=True),
                "",
            ),
        )
        return Reklama5PageRequest(scope=self, page=page, url=url)

    def accepts_redirect(
        self,
        request: Reklama5PageRequest,
        absolute_target_url: str,
    ) -> bool:
        trusted_target = _parse_search_url(absolute_target_url)
        if trusted_target is None:
            return False
        target, target_host, target_port = trusted_target
        if (
            target.scheme != self.scheme
            or target_host != self.host
            or target_port != self.port
        ):
            return False
        requested_query = urlsplit(request.url).query
        return _normalized_query(target.query) == _normalized_query(requested_query)


@dataclass(frozen=True, slots=True)
class Reklama5PageRequest:
    scope: Reklama5SearchScope
    page: int
    url: str


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
    try:
        while True:
            with requests.get(
                current_url,
                headers={
                    "Accept": "text/html",
                    "User-Agent": REKLAMA5_USER_AGENT,
                },
                timeout=budget.request_timeout(),
                allow_redirects=False,
                stream=True,
            ) as response:
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
    except (requests.ConnectionError, requests.Timeout) as error:
        raise FeedFetchError(
            REKLAMA5_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None
    except requests.RequestException as error:
        raise FeedFetchError(
            REKLAMA5_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None


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


def _parse_search_url(url: str) -> _ParsedSearchUrl | None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed_port = parsed.port
        port = 443 if parsed_port is None else parsed_port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or port != 443
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in _ALLOWED_PATHS
        or "#" in url
    ):
        return None
    if host is None:
        return None
    return parsed, host, port


def _normalized_query(query: str) -> Counter[tuple[str, str]]:
    return Counter(
        (_canonical_query_key(key), value)
        for key, value in parse_qsl(query, keep_blank_values=True)
    )


def _canonical_query_key(key: str) -> str:
    folded_key = key.casefold()
    return next(
        (
            canonical_key
            for owned_key, canonical_key in _CANONICAL_QUERY_KEYS
            if folded_key == owned_key
        ),
        key,
    )
