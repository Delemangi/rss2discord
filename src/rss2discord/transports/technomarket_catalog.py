"""Bounded, sequential server-rendered Technomarket catalog scraping."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import unescape
from time import monotonic
from typing import Any, Final, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as curl_requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import (
    PriceAmountValidationError,
    canonicalize_price_amount,
)
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    parse_retry_after,
)
from rss2discord.transports.technomarket_bounds import (
    MAX_TECHNOMARKET_PAGES,
    MAX_TECHNOMARKET_PRODUCTS,
    MAX_TECHNOMARKET_REQUESTS,
    MAX_TECHNOMARKET_RESPONSE_BYTES,
    MAX_TECHNOMARKET_SCAN_BYTES,
    MAX_TECHNOMARKET_SCAN_SECONDS,
    TECHNOMARKET_DISCOVERY_WINDOW,
    TECHNOMARKET_FEED_URL,
    TECHNOMARKET_HOST,
    TECHNOMARKET_LABEL,
    TECHNOMARKET_ORIGIN,
    TECHNOMARKET_USER_AGENT,
)
from rss2discord.transports.technomarket_models import TechnomarketProduct

__all__ = [
    "TECHNOMARKET_FEED_URL",
    "TechnomarketCatalogClient",
    "parse_mkd_price",
    "parse_product_card",
    "validate_technomarket_url",
]

_HTML_PARSER: Final = "html.parser"
_PRICE_RE: Final = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?(?!\d)",
)
_CATEGORY_PATH_RE: Final = re.compile(
    r"^/category/\d+/[a-z0-9]+(?:-[a-z0-9]+)*/?$",
)
_PRODUCT_PATH_RE: Final = re.compile(r"^/(?:product|products)/[^?#]+/?$")


@dataclass(slots=True)
class _ScanBudget:
    is_shutdown_requested: Callable[[], bool]
    started_at: float
    requests: int = 0
    response_bytes: int = 0

    @classmethod
    def start(cls, is_shutdown_requested: Callable[[], bool]) -> _ScanBudget:
        return cls(is_shutdown_requested, monotonic())

    def before_request(self) -> None:
        self._check()
        self.requests += 1
        if self.requests > MAX_TECHNOMARKET_REQUESTS:
            raise FeedFetchError(TECHNOMARKET_LABEL, "RequestLimitExceeded")

    def request_timeout(self) -> float:
        remaining = MAX_TECHNOMARKET_SCAN_SECONDS - (monotonic() - self.started_at)
        if remaining <= 0:
            raise FeedFetchError(TECHNOMARKET_LABEL, "ScanTimeLimitExceeded")
        return remaining

    def require_retry_delay(self, delay: float) -> None:
        if monotonic() - self.started_at + delay > MAX_TECHNOMARKET_SCAN_SECONDS:
            raise FeedFetchError(TECHNOMARKET_LABEL, "ScanTimeLimitExceeded")

    def add_bytes(self, amount: int) -> None:
        self.response_bytes += amount
        if self.response_bytes > MAX_TECHNOMARKET_SCAN_BYTES:
            raise FeedFetchError(TECHNOMARKET_LABEL, "ScanResponseTooLarge")
        self._check()

    def before_chunk(self) -> None:
        self._check()

    def after_request(self) -> None:
        self.before_chunk()

    def _check(self) -> None:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if monotonic() - self.started_at >= MAX_TECHNOMARKET_SCAN_SECONDS:
            raise FeedFetchError(TECHNOMARKET_LABEL, "ScanTimeLimitExceeded")


@dataclass(slots=True)
class _ContentCallbackState:
    budget: _ScanBudget | None
    content: bytearray
    abort_error: FeedFetchError | FeedFetchInterruptedError | None = None

    def write(self, chunk: bytes) -> int:
        if self.abort_error is not None:
            return CURL_WRITEFUNC_ERROR
        try:
            if self.budget is not None:
                self.budget.before_chunk()
            if len(self.content) + len(chunk) > MAX_TECHNOMARKET_RESPONSE_BYTES:
                self.abort_error = FeedFetchError(
                    TECHNOMARKET_LABEL,
                    "ResponseTooLarge",
                )
                return CURL_WRITEFUNC_ERROR
            self.content.extend(chunk)
            if self.budget is not None:
                self.budget.add_bytes(len(chunk))
        except (FeedFetchError, FeedFetchInterruptedError) as error:
            self.abort_error = error
            return CURL_WRITEFUNC_ERROR
        return len(chunk)


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


def parse_mkd_price(value: str) -> Decimal | None:
    """Parse one bounded MKD amount from a listing price label."""
    text = " ".join(unescape(value).replace("\xa0", " ").split())
    match = _PRICE_RE.search(text)
    if match is None:
        return None
    normalized = match.group(0).replace(" ", "")
    if "," in normalized:
        whole, fraction = normalized.split(",", 1)
        normalized = f"{whole.replace('.', '')}.{fraction}"
    else:
        normalized = normalized.replace(".", "")
    try:
        amount = Decimal(normalized)
        canonicalize_price_amount(amount)
    except (InvalidOperation, PriceAmountValidationError):
        return None
    return amount


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _first(node: Tag, selectors: tuple[str, ...]) -> Tag | None:
    for selector in selectors:
        candidate = node.select_one(selector)
        if isinstance(candidate, Tag):
            return candidate
    return None


def _price(node: Tag | None) -> Decimal | None:
    if node is None:
        return None
    return parse_mkd_price(
        _text(node) or str(node.get("data-price-amount") or node.get("content") or ""),
    )


def _safe_product_url(value: str) -> tuple[str, str]:
    absolute = urljoin(TECHNOMARKET_ORIGIN + "/", value.strip())
    try:
        parsed = urlsplit(absolute)
    except ValueError:
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidProductUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != TECHNOMARKET_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not _PRODUCT_PATH_RE.fullmatch(parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidProductUrl")
    path_parts = [part for part in parsed.path.split("/") if part]
    suffix = path_parts[1:]
    numeric_segments = [part for part in suffix if part.isdigit()]
    if len(numeric_segments) == 1:
        product_id = numeric_segments[0]
    else:
        numeric_tokens = re.findall(r"(?<!\d)\d+(?!\d)", suffix[-1] if suffix else "")
        if len(numeric_tokens) != 1:
            raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidProductIdentity")
        product_id = numeric_tokens[0]
    return urlunsplit(("https", TECHNOMARKET_HOST, parsed.path, "", "")), product_id


def _safe_image_url(value: str | None) -> str | None:
    if not value:
        return None
    absolute = urljoin(TECHNOMARKET_ORIGIN + "/", value.split(",", 1)[0].strip())
    try:
        parsed = urlsplit(absolute)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        return None
    return urlunsplit(("https", parsed.hostname or "", parsed.path, parsed.query, ""))


def parse_product_card(card: Tag | None) -> TechnomarketProduct:
    """Parse a complete product card, failing closed on identity errors."""
    if card is None:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedProduct")
    link = _first(
        card,
        ("a.product-link[href]", "a[href*='/product/']", "a[href*='/products/']"),
    )
    title = _first(
        card,
        (
            "[data-product-title]",
            ".product-title",
            ".product-name",
            ".product-item-name",
            "h2",
            "h3",
        ),
    )
    if link is None or not _text(title):
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedProduct")
    url, product_id = _safe_product_url(str(link.get("href", "")))
    regular_node = _first(
        card,
        (
            "[data-regular-price]",
            "[data-price-type='regular']",
            ".regular-price",
            ".price-regular",
            ".old-price",
            "del",
            ".product-price",
            ".price",
        ),
    )
    smart_node = _first(
        card,
        (
            "[data-smart-price]",
            "[data-price-type='smart']",
            ".smart-price",
            ".price-smart",
            ".smart",
            "[class*='smart']",
        ),
    )
    image = _first(
        card,
        ("img.product-image", "img[data-src]", "img[data-original]", "img[src]"),
    )
    manufacturer = (
        _text(_first(card, ("[data-manufacturer]", ".manufacturer", ".brand"))) or None
    )
    category_nodes = card.select(
        "[data-category], .category, .product-category, .categories a",
    )
    categories = tuple(
        dict.fromkeys(_text(node) for node in category_nodes if _text(node)),
    )
    return TechnomarketProduct(
        product_id=product_id,
        name=_text(title),
        url=url,
        image_url=_safe_image_url(
            str(
                image.get("data-src")
                or image.get("data-original")
                or image.get("src")
                or "",
            )
            if image
            else None,
        ),
        manufacturer=manufacturer,
        categories=categories,
        regular_price=_price(regular_node),
        smart_price=_price(smart_node),
    )


def _metadata_int(
    document: BeautifulSoup,
    names: tuple[str, ...],
    error: str,
) -> int | None:
    values: list[str] = []
    for name in names:
        values.extend(str(node.get(name, "")) for node in document.select(f"[{name}]"))
    if not values:
        return None
    if len(set(values)) != 1 or not values[0].isdigit() or int(values[0]) < 0:
        raise FeedFetchError(TECHNOMARKET_LABEL, error)
    return int(values[0])


def _page_count(document: BeautifulSoup) -> int:
    explicit = _metadata_int(
        document,
        ("data-total-pages", "data-page-count", "data-pages"),
        "MalformedPagination",
    )
    page_values: list[int] = []
    for link in document.select("a[href]"):
        query = parse_qsl(
            urlsplit(str(link.get("href", ""))).query,
            keep_blank_values=True,
        )
        page_params = [value for key, value in query if key == "page"]
        if page_params:
            if (
                len(page_params) != 1
                or not page_params[0].isdigit()
                or int(page_params[0]) < 1
            ):
                raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedPagination")
            page_values.append(int(page_params[0]))
    count = explicit or max(page_values, default=1)
    if count < 1:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedPagination")
    if count > MAX_TECHNOMARKET_PAGES:
        raise FeedFetchError(TECHNOMARKET_LABEL, "PageLimitExceeded")
    if explicit is not None and page_values and max(page_values) > explicit:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedPagination")
    return count


def _total_products(document: BeautifulSoup) -> int | None:
    return _metadata_int(
        document,
        ("data-total-products", "data-total-count", "data-product-count"),
        "MalformedCount",
    )


class TechnomarketCatalogClient:
    """Fetch a bounded discovery window or the complete configured category."""

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[TechnomarketProduct, ...]:
        root = validate_technomarket_url(url)
        budget = _ScanBudget.start(is_shutdown_requested)
        soup = BeautifulSoup(self._fetch_html(root, budget=budget), _HTML_PARSER)
        page_count = _page_count(soup)
        total = _total_products(soup)
        if total is None:
            raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedCount")
        products = self._parse_products(soup)
        if total is not None and total < len(products):
            raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidCount")
        if len(products) > TECHNOMARKET_DISCOVERY_WINDOW:
            raise FeedFetchError(TECHNOMARKET_LABEL, "DiscoveryLimitExceeded")
        if page_count == 1 and total is not None and total != len(products):
            raise FeedFetchError(TECHNOMARKET_LABEL, "IncompleteCatalog")
        return tuple(products)

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[TechnomarketProduct, ...]:
        root = validate_technomarket_url(url)
        budget = _ScanBudget.start(is_shutdown_requested)

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
        budget: _ScanBudget,
    ) -> tuple[TechnomarketProduct, ...]:
        first = BeautifulSoup(self._fetch_html(root, budget=budget), _HTML_PARSER)
        expected_pages = _page_count(first)
        expected_total = _total_products(first)
        if expected_total is None:
            raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedCount")
        if expected_total > MAX_TECHNOMARKET_PRODUCTS:
            raise FeedFetchError(TECHNOMARKET_LABEL, "ProductLimitExceeded")
        products: list[TechnomarketProduct] = []
        seen: set[str] = set()
        for page in range(1, expected_pages + 1):
            if page == 1:
                document = first
            else:
                document = BeautifulSoup(
                    self._fetch_html(self._page_url(root, page), budget=budget),
                    _HTML_PARSER,
                )
            if _page_count(document) != expected_pages:
                raise FeedFetchError(
                    TECHNOMARKET_LABEL,
                    "CatalogChanged",
                    retryable=True,
                )
            page_total = _total_products(document)
            if page_total is not None and page_total != expected_total:
                raise FeedFetchError(
                    TECHNOMARKET_LABEL,
                    "CatalogChanged",
                    retryable=True,
                )
            for product in self._parse_products(document):
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
        if expected_total is not None and len(products) != expected_total:
            raise FeedFetchError(
                TECHNOMARKET_LABEL,
                "IncompleteCatalog",
                retryable=True,
            )
        if expected_total is None and not products:
            raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedCount")
        return tuple(products)

    @staticmethod
    def _parse_products(document: BeautifulSoup) -> list[TechnomarketProduct]:
        cards = document.select(
            ".product-card, .product-item, article.product, [data-product-card]",
        )
        if not cards:
            if _total_products(document) == 0:
                return []
            raise FeedFetchError(TECHNOMARKET_LABEL, "EmptyPage")
        return [parse_product_card(card) for card in cards]

    @staticmethod
    def _page_url(root: str, page: int) -> str:
        return f"{root}?{urlencode({'page': page})}"

    @staticmethod
    def _fetch_html(url: str, *, budget: _ScanBudget | None = None) -> str:
        if budget is not None:
            budget.before_request()
        state = _ContentCallbackState(budget, bytearray())
        try:
            response = _perform_request(
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
        if state.abort_error is not None:
            raise state.abort_error
        if budget is not None:
            budget.after_request()
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
        content_length = response.headers.get("Content-Length")
        if content_length is not None and (
            not content_length.isdigit()
            or int(content_length) > MAX_TECHNOMARKET_RESPONSE_BYTES
        ):
            raise FeedFetchError(TECHNOMARKET_LABEL, "ResponseTooLarge")
        try:
            return bytes(state.content).decode(
                response.encoding or "utf-8",
                errors="strict",
            )
        except UnicodeDecodeError:
            raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidResponse") from None
