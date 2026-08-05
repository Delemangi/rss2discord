"""Bounded HTTP boundary for Gjirafa50 catalog pages."""

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from http.cookiejar import DefaultCookiePolicy
from threading import Lock
from types import TracebackType
from typing import Final, Protocol, Self
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.gjirafa50_models import (
    Gjirafa50CatalogPage,
    Gjirafa50PriceRange,
)
from rss2discord.transports.gjirafa50_parser import (
    GJIRAFA50_LABEL,
    GJIRAFA50_ORIGIN,
    parse_gjirafa50_page,
)

GJIRAFA50_SEARCH_URL: Final = f"{GJIRAFA50_ORIGIN}/product/search"
GJIRAFA50_RESPONSE_BYTES: Final = 5 * 1024 * 1024
GJIRAFA50_STREAM_CHUNK_BYTES: Final = 64 * 1024
GJIRAFA50_USER_AGENT: Final = "Mozilla/5.0 (compatible; rss2discord/0.1)"
MAX_GJIRAFA50_REDIRECTS: Final = 3
GJIRAFA50_REQUEST_INTERVAL_SECONDS: Final = 0.05


class Gjirafa50HttpBudget(Protocol):
    deadline: float

    def before_request(self) -> None: ...

    def consume_bytes(self, response_bytes: int) -> None: ...


class _HostPacer:
    def __init__(self) -> None:
        self._lock = Lock()
        self._last_request_at: float | None = None

    def before_request(self, budget: Gjirafa50HttpBudget) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                delay = GJIRAFA50_REQUEST_INTERVAL_SECONDS - (
                    now - self._last_request_at
                )
                if delay > 0:
                    time.sleep(delay)
            budget.before_request()
            self._last_request_at = time.monotonic()


_HOST_PACER: Final = _HostPacer()


@dataclass(frozen=True, slots=True)
class Gjirafa50PageRequest:
    page: int
    budget: Gjirafa50HttpBudget
    price_range: Gjirafa50PriceRange | None = None
    order_by: int | None = None


@dataclass(frozen=True, slots=True)
class FetchedGjirafa50Page:
    page: Gjirafa50CatalogPage
    response_bytes: int


class Gjirafa50HttpClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.cookies.set_policy(
            DefaultCookiePolicy(
                blocked_domains=("gjirafa50.mk", ".gjirafa50.mk"),
            ),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self._session.close()

    @staticmethod
    def normalize_root_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise FeedFetchError(GJIRAFA50_LABEL, "InvalidUrl") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "gjirafa50.mk"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise FeedFetchError(GJIRAFA50_LABEL, "InvalidUrl")
        return urlunsplit(("https", "gjirafa50.mk", "/", "", ""))

    def fetch_page(
        self,
        root_url: str,
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        params: dict[str, str | int] = {
            "pagenumber": request.page,
            "is": "true",
        }
        if request.order_by is not None:
            params["orderby"] = request.order_by
        if request.price_range is not None:
            params["price"] = str(request.price_range)
        content, response_bytes = self._request(
            params,
            root_url=self.normalize_root_url(root_url),
            budget=request.budget,
        )
        page = parse_gjirafa50_page(content, observed_at)
        if time.monotonic() >= request.budget.deadline:
            raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")
        return FetchedGjirafa50Page(page, response_bytes)

    def _request(
        self,
        params: dict[str, str | int],
        *,
        root_url: str,
        budget: Gjirafa50HttpBudget,
    ) -> tuple[bytes, int]:
        current_url = GJIRAFA50_SEARCH_URL
        consumed_bytes = 0
        try:
            for _ in range(MAX_GJIRAFA50_REDIRECTS + 1):
                _HOST_PACER.before_request(budget)
                remaining_seconds = budget.deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")
                response_context = self._session.get(
                    current_url,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "Referer": root_url,
                        "User-Agent": GJIRAFA50_USER_AGENT,
                    },
                    timeout=min(30, remaining_seconds),
                    stream=True,
                    allow_redirects=False,
                )
                with response_context as response:
                    content = _read_content(
                        response,
                        budget=budget,
                    )
                    consumed_bytes += len(content)
                    if time.monotonic() >= budget.deadline:
                        raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if location is None:
                            raise FeedFetchError(GJIRAFA50_LABEL, "InvalidRedirect")
                        current_url = _same_origin_redirect(current_url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except requests.HTTPError:
                        status = response.status_code
                        raise FeedFetchError(
                            GJIRAFA50_LABEL,
                            "HTTPError",
                            status_code=status,
                            retryable=status in {408, 429} or 500 <= status < 600,
                            retry_after=parse_retry_after(response.headers.get("Retry-After")),
                        ) from None
                    return content, consumed_bytes
            raise FeedFetchError(GJIRAFA50_LABEL, "TooManyRedirects")
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as error:
            raise FeedFetchError(
                GJIRAFA50_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(GJIRAFA50_LABEL, type(error).__name__) from None


def _same_origin_redirect(current_url: str, location: str) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        redirected = urlsplit(redirected_url)
        port = redirected.port
    except ValueError:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != "https"
        or redirected.hostname != "gjirafa50.mk"
        or port is not None
        or redirected.username is not None
        or redirected.password is not None
        or redirected.path != "/product/search"
        or redirected.fragment
    ):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidRedirect")
    return redirected_url


def _read_content(
    response: requests.Response,
    *,
    budget: Gjirafa50HttpBudget,
) -> bytes:
    content = bytearray()
    response_bytes = sum(
        len(name.encode("latin-1")) + len(value.encode("latin-1")) + 4
        for name, value in response.headers.items()
    )
    budget.consume_bytes(response_bytes)
    if response_bytes > GJIRAFA50_RESPONSE_BYTES:
        raise FeedFetchError(GJIRAFA50_LABEL, "ResponseTooLarge")
    chunks: Iterator[bytes] = response.iter_content(GJIRAFA50_STREAM_CHUNK_BYTES)
    for chunk in chunks:
        if time.monotonic() >= budget.deadline:
            raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")
        budget.consume_bytes(len(chunk))
        response_bytes += len(chunk)
        if response_bytes > GJIRAFA50_RESPONSE_BYTES:
            raise FeedFetchError(GJIRAFA50_LABEL, "ResponseTooLarge")
        content.extend(chunk)
    return bytes(content)
