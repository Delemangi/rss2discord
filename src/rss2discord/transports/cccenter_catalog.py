"""Bounded, server-rendered CCCenter WooCommerce catalog scraping."""

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
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.cccenter_bounds import (
    CCCENTER_FEED_URL,
    CCCENTER_LABEL,
    CCCENTER_ORIGIN,
    CCCENTER_SHOP_PATH,
    CCCENTER_USER_AGENT,
    MAX_CCCENTER_PAGES,
    MAX_CCCENTER_PRODUCTS,
    MAX_CCCENTER_REQUESTS,
    MAX_CCCENTER_RESPONSE_BYTES,
    MAX_CCCENTER_SCAN_BYTES,
    MAX_CCCENTER_SCAN_SECONDS,
)
from rss2discord.transports.cccenter_models import (
    CCCenterListing,
    CCCenterPriceStatus,
    CCCenterProduct,
)

__all__ = [
    "CCCENTER_FEED_URL",
    "CCCenterCatalogClient",
    "parse_mkd_price",
    "parse_product_detail",
    "parse_product_listing",
    "validate_cccenter_url",
]

_PRICE_RE: Final = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?(?!\d)",
)


@dataclass(slots=True)
class _ScanBudget:
    is_shutdown_requested: Callable[[], bool]
    started_at: float
    requests: int = 0
    response_bytes: int = 0

    @classmethod
    def start(cls, is_shutdown_requested: Callable[[], bool]) -> _ScanBudget:
        return cls(is_shutdown_requested=is_shutdown_requested, started_at=monotonic())

    def before_request(self) -> None:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if monotonic() - self.started_at >= MAX_CCCENTER_SCAN_SECONDS:
            raise FeedFetchError(CCCENTER_LABEL, "ScanTimeLimitExceeded")
        self.requests += 1
        if self.requests > MAX_CCCENTER_REQUESTS:
            raise FeedFetchError(CCCENTER_LABEL, "RequestLimitExceeded")

    def request_timeout(self) -> float:
        remaining = MAX_CCCENTER_SCAN_SECONDS - (monotonic() - self.started_at)
        if remaining <= 0:
            raise FeedFetchError(CCCENTER_LABEL, "ScanTimeLimitExceeded")
        return remaining

    def require_retry_delay(self, delay: float) -> None:
        if monotonic() - self.started_at + delay > MAX_CCCENTER_SCAN_SECONDS:
            raise FeedFetchError(CCCENTER_LABEL, "ScanTimeLimitExceeded")

    def add_bytes(self, amount: int) -> None:
        self.response_bytes += amount
        if self.response_bytes > MAX_CCCENTER_SCAN_BYTES:
            raise FeedFetchError(CCCENTER_LABEL, "ScanResponseTooLarge")
        if monotonic() - self.started_at >= MAX_CCCENTER_SCAN_SECONDS:
            raise FeedFetchError(CCCENTER_LABEL, "ScanTimeLimitExceeded")

    def before_chunk(self) -> None:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if monotonic() - self.started_at >= MAX_CCCENTER_SCAN_SECONDS:
            raise FeedFetchError(CCCENTER_LABEL, "ScanTimeLimitExceeded")

    def after_request(self) -> None:
        self.before_chunk()


@dataclass(slots=True)
class _ContentCallbackState:
    """Collect one response while preserving callback abort causes."""

    budget: _ScanBudget | None
    content: bytearray
    abort_error: FeedFetchError | FeedFetchInterruptedError | None = None

    @classmethod
    def start(cls, budget: _ScanBudget | None) -> _ContentCallbackState:
        return cls(budget=budget, content=bytearray())

    def write(self, chunk: bytes) -> int:
        if self.abort_error is not None:
            return CURL_WRITEFUNC_ERROR
        try:
            if self.budget is not None:
                self.budget.before_chunk()
            if len(self.content) + len(chunk) > MAX_CCCENTER_RESPONSE_BYTES:
                self.abort_error = FeedFetchError(CCCENTER_LABEL, "ResponseTooLarge")
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


def _validate_response(response: _HttpResponse) -> None:
    if 300 <= response.status_code < 400:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidRedirect")
    try:
        response.raise_for_status()
    except curl_requests.exceptions.HTTPError:
        status_code = response.status_code
        raise FeedFetchError(
            CCCENTER_LABEL,
            "HTTPError",
            status_code=status_code,
            retryable=(status_code == 429 or 500 <= status_code < 600),
        ) from None
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_CCCENTER_RESPONSE_BYTES:
            raise FeedFetchError(CCCENTER_LABEL, "ResponseTooLarge")


def validate_cccenter_url(url: str) -> str:
    """Validate the one supported CCCenter listing URL."""
    if url != CCCENTER_FEED_URL:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidUrl")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "cccenter.mk"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != CCCENTER_SHOP_PATH
        or parsed.fragment
        or parse_qsl(parsed.query, keep_blank_values=True) != [("orderby", "date")]
    ):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidUrl")
    return CCCENTER_FEED_URL


def parse_mkd_price(value: str) -> Decimal | None:
    """Parse a single Macedonian denar display amount, or return ``None``."""
    text = " ".join(unescape(value).replace("\xa0", " ").split())
    text = re.sub(r"(?:ден(?:ари)?|MKD)\.?$", "", text, flags=re.IGNORECASE).strip()
    match = _PRICE_RE.fullmatch(text)
    if match is None:
        return None
    normalized = text.replace(" ", "")
    if "," in normalized:
        whole, fraction = normalized.split(",", 1)
        whole = whole.replace(".", "")
        normalized = f"{whole}.{fraction}"
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


def _first_price(node: Tag | None) -> Decimal | None:
    if node is None:
        return None
    return parse_mkd_price(_text(node))


def _prices(container: Tag | None) -> tuple[Decimal | None, Decimal | None]:
    if container is None:
        return None, None
    sale = _first_price(container.select_one("ins"))
    original = _first_price(container.select_one("del"))
    if sale is not None:
        return sale, original
    return _first_price(container), None


def _price_status(
    container: Tag | None,
    current_price: Decimal | None,
    *,
    is_variable: bool = False,
) -> CCCenterPriceStatus:
    if is_variable:
        return "variable"
    if container is not None:
        del_node = container.select_one("del")
        ins_node = container.select_one("ins")
        if any(
            _has_range_evidence(_text(node))
            for node in (container, del_node, ins_node)
            if node is not None
        ):
            return "range"
        if any(
            node is not None and _first_price(node) is None
            for node in (del_node, ins_node)
        ):
            return "unpriced"
    if current_price is not None:
        return "scalar"
    text = _text(container)
    if _has_range_evidence(text):
        return "range"
    return "unpriced"


def _has_range_evidence(text: str) -> bool:
    return bool(
        re.search(r"(?:–|—|\s-\s|\bto\b)", text, flags=re.IGNORECASE),
    )


def _merge_price_status(
    listing_status: CCCenterPriceStatus,
    detail_status: CCCenterPriceStatus,
) -> CCCenterPriceStatus:
    """Keep any non-scalar classification found by either product view."""
    for status in ("variable", "range", "unpriced"):
        if status in {listing_status, detail_status}:
            return status
    return "scalar"


def _safe_product_url(url: str) -> str:
    try:
        raw = urlsplit(url)
        absolute = urljoin(CCCENTER_ORIGIN + "/", url)
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl") from None
    raw_path = raw.path
    if not raw.scheme and not raw.netloc and not url.startswith("/product/"):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "cccenter.mk"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not re.fullmatch(r"/product/[a-z0-9]+(?:-[a-z0-9]+)*/", raw_path)
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl")
    return urlunsplit(("https", "cccenter.mk", raw_path, "", ""))


def _safe_image_url(url: str | None) -> str | None:
    if not url:
        return None
    absolute = urljoin(
        CCCENTER_ORIGIN + "/",
        url.split(",", 1)[0].strip().split(" ", 1)[0],
    )
    parsed = urlsplit(absolute)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "cccenter.mk"
        or parsed.port not in {None, 443}
    ):
        return None
    return urlunsplit(("https", "cccenter.mk", parsed.path, parsed.query, ""))


def parse_product_listing(card: Tag | None) -> CCCenterListing:
    if card is None:
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    heading = card.select_one("h2.woocommerce-loop-product__title")
    link = card.select_one("a[href]")
    name = _text(heading)
    if not name or link is None:
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    product_url = _safe_product_url(str(link.get("href", "")))
    current_price, original_price = _prices(card.select_one(".price"))
    price_status = _price_status(
        card.select_one(".price"),
        current_price,
        is_variable="product-type-variable" in str(card.get("class") or ""),
    )
    image = card.select_one("img")
    image_url = _safe_image_url(
        str(image.get("src") or image.get("data-src") or image.get("srcset") or "")
        if image
        else None,
    )
    return CCCenterListing(
        product_id=product_url,
        name=name,
        url=product_url,
        current_price=current_price,
        original_price=original_price,
        image_url=image_url,
        price_status=price_status,
    )


def parse_product_detail(
    document: BeautifulSoup,
    listing: CCCenterListing,
) -> CCCenterProduct:
    heading = document.select_one("h1.product_title")
    if not _text(heading):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    price_container = document.select_one(".summary .price, .product .price, .price")
    current_price, original_price = _prices(price_container)
    is_variable = (
        document.select_one(
            ".variations_form, form.variations_form, .product-type-variable",
        )
        is not None
    )
    price_status = _price_status(
        price_container,
        current_price,
        is_variable=is_variable,
    )
    sku = _text(document.select_one(".sku"))
    stock = document.select_one(".stock")
    is_in_stock = stock is None or "out-of-stock" not in str(stock.get("class") or "")
    categories = tuple(
        category
        for category in (_text(node) for node in document.select(".posted_in a"))
        if category
    )
    image_url = listing.image_url
    for image in document.select(".woocommerce-product-gallery img"):
        image_url = (
            _safe_image_url(
                str(
                    image.get("src")
                    or image.get("data-src")
                    or image.get("srcset")
                    or "",
                ),
            )
            or image_url
        )
        if image_url:
            break
    return CCCenterProduct(
        product_id=listing.product_id,
        name=_text(heading),
        url=listing.url,
        sku=sku,
        current_price=current_price
        if current_price is not None
        else listing.current_price,
        original_price=original_price
        if original_price is not None
        else listing.original_price,
        image_url=image_url,
        categories=categories,
        is_in_stock=is_in_stock,
        price_status=_merge_price_status(listing.price_status, price_status),
    )


class CCCenterCatalogClient:
    """Fetch CCCenter's newest bounded HTML catalog window."""

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        return self.fetch_catalog(url, is_shutdown_requested=is_shutdown_requested)

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        validate_cccenter_url(url)
        budget = _ScanBudget.start(is_shutdown_requested)
        if retry_policy is None:
            return self._scan_catalog(url, is_shutdown_requested, budget)
        return retry_policy.execute(
            lambda: self._scan_catalog(url, is_shutdown_requested, budget),
            retry_guard=budget.require_retry_delay,
        )

    def _scan_catalog(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool],
        budget: _ScanBudget,
    ) -> tuple[CCCenterProduct, ...]:
        first_url = validate_cccenter_url(url)
        first_html = self._fetch_html(first_url, budget=budget)
        first_soup = BeautifulSoup(first_html, "html.parser")
        page_count = self._page_count(first_soup)
        products: list[CCCenterProduct] = []
        seen_ids: set[str] = set()
        for page in range(1, page_count + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            page_url = first_url if page == 1 else self._page_url(page)
            html = (
                first_html if page == 1 else self._fetch_html(page_url, budget=budget)
            )
            soup = first_soup if page == 1 else BeautifulSoup(html, "html.parser")
            cards = soup.select("li.product")
            if not cards:
                raise FeedFetchError(CCCENTER_LABEL, "EmptyPage")
            for card in cards:
                listing = parse_product_listing(card)
                if listing.product_id in seen_ids:
                    raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
                seen_ids.add(listing.product_id)
                if len(seen_ids) > MAX_CCCENTER_PRODUCTS:
                    raise FeedFetchError(CCCENTER_LABEL, "ProductLimitExceeded")
                detail_html = self._fetch_html(listing.url, budget=budget)
                product = parse_product_detail(
                    BeautifulSoup(detail_html, "html.parser"),
                    listing,
                )
                products.append(product)
        return tuple(products)

    @staticmethod
    def _page_count(document: BeautifulSoup) -> int:
        pages = [
            int(value)
            for link in document.select("a.page-numbers[href]")
            if (
                value := dict(parse_qsl(urlsplit(str(link["href"])).query)).get(
                    "product-page",
                )
            )
            and value.isdigit()
        ]
        page_count = max(pages, default=1)
        if page_count > MAX_CCCENTER_PAGES:
            raise FeedFetchError(CCCENTER_LABEL, "PageLimitExceeded")
        return page_count

    @staticmethod
    def _page_url(page: int) -> str:
        return f"{CCCENTER_ORIGIN}{CCCENTER_SHOP_PATH}?{urlencode({'orderby': 'date', 'product-page': page})}"

    @staticmethod
    def _fetch_html(
        url: str,
        *,
        budget: _ScanBudget | None = None,
    ) -> str:
        if budget is not None:
            budget.before_request()
        callback_state = _ContentCallbackState.start(budget)

        try:
            timeout = budget.request_timeout() if budget is not None else 30.0
            response = _perform_request(
                url,
                headers={"Accept": "text/html", "User-Agent": CCCENTER_USER_AGENT},
                timeout=timeout,
                stream=False,
                allow_redirects=False,
                content_callback=callback_state.write,
            )
        except FeedFetchError:
            raise
        except (curl_requests.exceptions.RequestException, ValueError) as error:
            if callback_state.abort_error is not None:
                raise callback_state.abort_error from None
            raise FeedFetchError(
                CCCENTER_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        if callback_state.abort_error is not None:
            raise callback_state.abort_error from None
        if budget is not None:
            budget.after_request()
        _validate_response(response)
        try:
            return bytes(callback_state.content).decode(
                response.encoding or "utf-8",
                errors="strict",
            )
        except UnicodeDecodeError:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse") from None
