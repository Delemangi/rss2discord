"""Bounded, sequential server-rendered Technomarket catalog scraping."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.technomarket.bounds import (
    MAX_TECHNOMARKET_PRODUCTS,
    MAX_TECHNOMARKET_REQUESTS,
    MAX_TECHNOMARKET_RESPONSE_BYTES,
    MAX_TECHNOMARKET_SCAN_BYTES,
    MAX_TECHNOMARKET_SCAN_SECONDS,
    TECHNOMARKET_FEED_URL,
    TECHNOMARKET_HOST,
    TECHNOMARKET_LABEL,
    TECHNOMARKET_USER_AGENT,
)
from rss2discord.providers.technomarket.models import TechnomarketProduct
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    parse_retry_after,
)
from rss2discord.transports.bounded_http import validate_content_length
from rss2discord.transports.catalog_http import BoundedContentCallback
from rss2discord.transports.scan_budget import ScanBudget, ScanLimits

from .parser import (
    _HTML_PARSER,
    _page_count,
    _page_info,
    _PageInfo,
    _total_products,
    parse_product_card,
)

__all__ = [
    "TECHNOMARKET_FEED_URL",
    "TechnomarketCatalogClient",
    "validate_technomarket_url",
]

_CATEGORY_PATH_RE: Final = re.compile(
    r"^/category/\d+/[a-z0-9]+(?:-[a-z0-9]+)*/?$",
)

TECHNOMARKET_SCAN_LIMITS: Final = ScanLimits(
    label=TECHNOMARKET_LABEL,
    max_seconds=MAX_TECHNOMARKET_SCAN_SECONDS,
    max_requests=MAX_TECHNOMARKET_REQUESTS,
    max_bytes=MAX_TECHNOMARKET_SCAN_BYTES,
)


class _HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    encoding: str | None

    def raise_for_status(self) -> None: ...


def _perform_request(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    allow_redirects: bool,
    stream: bool,
    content_callback: Callable[[bytes], int],
) -> Any:  # noqa: ANN401
    return curl_requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=allow_redirects,
        stream=stream,
        content_callback=content_callback,
    )


def validate_technomarket_url(url: str) -> str:
    """Validate one credential-free HTTPS Technomarket category root."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != TECHNOMARKET_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not _CATEGORY_PATH_RE.fullmatch(parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidUrl")
    return urlunsplit(("https", TECHNOMARKET_HOST, parsed.path, "", ""))


class TechnomarketCatalogClient:
    """Fetch the complete configured category within fixed scan bounds."""

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[TechnomarketProduct, ...]:
        return self.fetch_catalog(
            url,
            is_shutdown_requested=is_shutdown_requested,
        )

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[TechnomarketProduct, ...]:
        root = validate_technomarket_url(url)
        budget = ScanBudget.start(TECHNOMARKET_SCAN_LIMITS, is_shutdown_requested)

        def operation() -> tuple[TechnomarketProduct, ...]:
            return self._scan_catalog(root, budget)

        if retry_policy is not None:
            return retry_policy.execute(
                operation,
                retry_guard=budget.require_retry_delay,
            )
        return operation()

    def _scan_catalog(
        self,
        root: str,
        budget: ScanBudget,
    ) -> tuple[TechnomarketProduct, ...]:
        first = BeautifulSoup(self._fetch_html(root, budget=budget), _HTML_PARSER)
        expected_pages = _page_count(first)
        expected_total = _total_products(first)
        if expected_total > MAX_TECHNOMARKET_PRODUCTS:
            raise FeedFetchError(TECHNOMARKET_LABEL, "ProductLimitExceeded")
        products: list[TechnomarketProduct] = []
        seen: set[str] = set()
        observed_at = datetime.now(UTC)
        scanned_count = 0
        for page in range(1, expected_pages + 1):
            document = self._page_document(root, page, first, budget)
            page_info = self._validate_page(document, expected_pages, expected_total)
            page_products = self._parse_products(document, observed_at=observed_at)
            scanned_count = self._append_page_products(
                products,
                seen,
                page_products,
                page_info,
                scanned_count,
            )
        if len(products) != expected_total:
            raise FeedFetchError(
                TECHNOMARKET_LABEL,
                "IncompleteCatalog",
                retryable=True,
            )
        return tuple(products)

    def _page_document(
        self,
        root: str,
        page: int,
        first: BeautifulSoup,
        budget: ScanBudget,
    ) -> BeautifulSoup:
        if page == 1:
            return first
        return BeautifulSoup(
            self._fetch_html(self._page_url(root, page), budget=budget),
            _HTML_PARSER,
        )

    @staticmethod
    def _validate_page(
        document: BeautifulSoup,
        expected_pages: int,
        expected_total: int,
    ) -> _PageInfo:
        page_info = _page_info(document)
        if _page_count(document) != expected_pages or page_info.total != expected_total:
            raise FeedFetchError(
                TECHNOMARKET_LABEL,
                "CatalogChanged",
                retryable=True,
            )
        return page_info

    @staticmethod
    def _append_page_products(
        products: list[TechnomarketProduct],
        seen: set[str],
        page_products: list[TechnomarketProduct],
        page_info: _PageInfo,
        scanned_count: int,
    ) -> int:
        expected_count = page_info.last - page_info.first + 1
        if (
            len(page_products) != expected_count
            or page_info.first != scanned_count + 1
            or page_info.last != scanned_count + len(page_products)
        ):
            raise FeedFetchError(
                TECHNOMARKET_LABEL,
                "IncompleteCatalog",
                retryable=True,
            )
        for product in page_products:
            if product.product_id in seen:
                raise FeedFetchError(
                    TECHNOMARKET_LABEL,
                    "DuplicateProduct",
                    retryable=True,
                )
            seen.add(product.product_id)
            if len(seen) > MAX_TECHNOMARKET_PRODUCTS:
                raise FeedFetchError(TECHNOMARKET_LABEL, "ProductLimitExceeded")
            products.append(product)
        return scanned_count + len(page_products)

    @staticmethod
    def _parse_products(
        document: BeautifulSoup,
        *,
        observed_at: datetime | None = None,
    ) -> list[TechnomarketProduct]:
        cards = document.select(
            "li.product-fix[data-id], .product-card, .product-item, article.product, [data-product-card]",
        )
        if not cards:
            if _total_products(document) == 0:
                return []
            raise FeedFetchError(TECHNOMARKET_LABEL, "EmptyPage")
        return [parse_product_card(card, observed_at=observed_at) for card in cards]

    @staticmethod
    def _page_url(root: str, page: int) -> str:
        return f"{root}?page={page}"

    @staticmethod
    def _fetch_html(url: str, *, budget: ScanBudget | None = None) -> str:
        if budget is not None:
            budget.before_request()
        state = BoundedContentCallback.start(
            budget,
            max_bytes=MAX_TECHNOMARKET_RESPONSE_BYTES,
            label=TECHNOMARKET_LABEL,
        )
        response = TechnomarketCatalogClient._request_html(url, budget, state)
        if state.abort_error is not None:
            raise state.abort_error
        if budget is not None:
            budget.after_request()
        return _decode_html(response, state.content)

    @staticmethod
    def _request_html(
        url: str,
        budget: ScanBudget | None,
        state: BoundedContentCallback,
    ) -> Any:  # noqa: ANN401
        try:
            return _perform_request(
                url,
                headers={"Accept": "text/html", "User-Agent": TECHNOMARKET_USER_AGENT},
                timeout=budget.request_timeout() if budget is not None else 30.0,
                allow_redirects=False,
                stream=False,
                content_callback=state.write,
            )
        except (FeedFetchError, FeedFetchInterruptedError):
            raise
        except (curl_requests.exceptions.RequestException, ValueError) as error:
            if state.abort_error is not None:
                raise state.abort_error from None
            raise FeedFetchError(
                TECHNOMARKET_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None


def _decode_html(response: _HttpResponse, content: bytearray) -> str:
    if 300 <= response.status_code < 400:
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidRedirect")
    try:
        response.raise_for_status()
    except curl_requests.exceptions.HTTPError:
        status = response.status_code
        raise FeedFetchError(
            TECHNOMARKET_LABEL,
            "HTTPError",
            status_code=status,
            retryable=status == 429 or 500 <= status < 600,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        ) from None
    validate_content_length(
        response,
        label=TECHNOMARKET_LABEL,
        max_bytes=MAX_TECHNOMARKET_RESPONSE_BYTES,
        invalid_cause="ResponseTooLarge",
    )
    try:
        return bytes(content).decode(
            response.encoding or "utf-8",
            errors="strict",
        )
    except UnicodeDecodeError:
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidResponse") from None
