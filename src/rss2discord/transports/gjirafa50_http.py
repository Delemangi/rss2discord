"""Bounded HTTP boundary for Gjirafa50 catalog pages."""

import math
import time
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from types import TracebackType
from typing import Final, Protocol, Self
from urllib.parse import urljoin, urlsplit, urlunsplit

from curl_cffi import requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.retries import FeedFetchInterruptedError, parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.gjirafa50_models import (
    Gjirafa50CatalogPage,
    Gjirafa50PriceRange,
)
from rss2discord.transports.gjirafa50_parser import (
    GJIRAFA50_LABEL,
    parse_gjirafa50_page,
)
from rss2discord.transports.gjirafa50_session import (
    Gjirafa50HttpResponse,
    Gjirafa50HttpSession,
    create_gjirafa50_session,
)

GJIRAFA50_HOSTS: Final = frozenset({"gjirafa50.com", "gjirafa50.mk"})
GJIRAFA50_RESPONSE_BYTES: Final = 5 * 1024 * 1024
GJIRAFA50_USER_AGENT: Final = "Mozilla/5.0 (compatible; rss2discord/0.1)"
MAX_GJIRAFA50_REDIRECTS: Final = 3
GJIRAFA50_REQUEST_INTERVAL_SECONDS: Final = 0.05
MAX_GJIRAFA50_TRANSFER_SECONDS: Final = 30


class Gjirafa50HttpBudget(Protocol):
    deadline: float

    def before_request(self) -> None: ...

    def check_active(self) -> None: ...

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


class _BoundedContent:
    """Accumulate one transfer while preserving its exact local abort cause."""

    def __init__(self, budget: Gjirafa50HttpBudget) -> None:
        self.content = bytearray()
        self.budget = budget
        self.response_bytes = 0
        self.abort_error: FeedFetchError | FeedFetchInterruptedError | None = None

    def write(self, chunk: bytes) -> int:
        if self._consume(len(chunk)) == CURL_WRITEFUNC_ERROR:
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)

    def write_header(self, line: bytes) -> int:
        return self._consume(len(line))

    def _consume(self, byte_count: int) -> int:
        try:
            self.budget.check_active()
            self.budget.consume_bytes(byte_count)
        except (FeedFetchError, FeedFetchInterruptedError) as error:
            self.abort_error = error
            return CURL_WRITEFUNC_ERROR
        self.response_bytes += byte_count
        if self.response_bytes > GJIRAFA50_RESPONSE_BYTES:
            self.abort_error = FeedFetchError(GJIRAFA50_LABEL, "ResponseTooLarge")
            return CURL_WRITEFUNC_ERROR
        return byte_count


def _create_session() -> Gjirafa50HttpSession:
    return create_gjirafa50_session()


class Gjirafa50HttpClient:
    def __init__(self, session: Gjirafa50HttpSession | None = None) -> None:
        self._session = _create_session() if session is None else session

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
            or parsed.hostname not in GJIRAFA50_HOSTS
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise FeedFetchError(GJIRAFA50_LABEL, "InvalidUrl")
        return urlunsplit(("https", parsed.hostname, "/", "", ""))

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
        normalized_root_url = self.normalize_root_url(root_url)
        content, response_bytes = self._request(
            params,
            root_url=normalized_root_url,
            budget=request.budget,
        )
        page = parse_gjirafa50_page(content, observed_at, normalized_root_url)
        request.budget.check_active()
        return FetchedGjirafa50Page(page, response_bytes)

    def _request(
        self,
        params: dict[str, str | int],
        *,
        root_url: str,
        budget: Gjirafa50HttpBudget,
    ) -> tuple[bytes, int]:
        current_url = urljoin(root_url, "product/search")
        consumed_bytes = 0
        content = _BoundedContent(budget)
        for _ in range(MAX_GJIRAFA50_REDIRECTS + 1):
            _HOST_PACER.before_request(budget)
            remaining_seconds = budget.deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise FeedFetchError(GJIRAFA50_LABEL, "ScanTimeLimitExceeded")
            content = _BoundedContent(budget)
            try:
                response = self._session.get(
                    current_url,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "Referer": root_url,
                        "User-Agent": GJIRAFA50_USER_AGENT,
                    },
                    allow_redirects=False,
                    content_callback=content.write,
                    header_callback=content.write_header,
                    timeout_ms=max(
                        1,
                        math.ceil(
                            min(remaining_seconds, MAX_GJIRAFA50_TRANSFER_SECONDS)
                            * 1000,
                        ),
                    ),
                )
            except requests.RequestsError as error:
                if content.abort_error is not None:
                    raise content.abort_error from None
                budget.check_active()
                raise FeedFetchError(
                    GJIRAFA50_LABEL,
                    type(error).__name__,
                    retryable=True,
                ) from None
            if content.abort_error is not None:
                raise content.abort_error
            consumed_bytes += content.response_bytes
            budget.check_active()
            if 300 <= response.status_code < 400:
                location = _header(response, "location")
                if location is None:
                    raise FeedFetchError(GJIRAFA50_LABEL, "InvalidRedirect")
                current_url = _same_origin_redirect(current_url, location)
                continue
            if response.status_code >= 400:
                status = response.status_code
                raise FeedFetchError(
                    GJIRAFA50_LABEL,
                    "HTTPError",
                    status_code=status,
                    retryable=status in {408, 429} or 500 <= status < 600,
                    retry_after=parse_retry_after(_header(response, "retry-after")),
                )
            return bytes(content.content), consumed_bytes
        raise FeedFetchError(GJIRAFA50_LABEL, "TooManyRedirects")


def _same_origin_redirect(current_url: str, location: str) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        current = urlsplit(current_url)
        redirected = urlsplit(redirected_url)
        port = redirected.port
    except ValueError:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != "https"
        or redirected.hostname != current.hostname
        or port is not None
        or redirected.username is not None
        or redirected.password is not None
        or redirected.path != "/product/search"
        or redirected.query
        or redirected.fragment
    ):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidRedirect")
    return redirected_url


def _header(response: Gjirafa50HttpResponse, name: str) -> str | None:
    expected = name.casefold()
    return next(
        (
            value
            for key, value in response.headers.items()
            if key.casefold() == expected
        ),
        None,
    )
