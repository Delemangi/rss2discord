"""Bounded, server-rendered CCCenter WooCommerce catalog scraping."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import unescape
from time import monotonic
from typing import Any, Final, Protocol
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as curl_requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import (
    PriceAmountValidationError,
    canonicalize_price_amount,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.catalog_http import BoundedContentCallback
from rss2discord.transports.cccenter_bounds import (
    CCCENTER_FEED_URL,
    CCCENTER_LABEL,
    CCCENTER_ORIGIN,
    CCCENTER_PRODUCTS_PER_PAGE,
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
    "MAX_CCCENTER_DETAIL_PRODUCTS",
    "CCCenterCatalogClient",
    "parse_mkd_price",
    "parse_product_detail",
    "parse_product_listing",
    "validate_cccenter_url",
]

_PRICE_RE: Final = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?(?!\d)",
)
_CCCENTER_HOST: Final = "cccenter.mk"
_HTML_PARSER: Final = "html.parser"
MAX_CCCENTER_DETAIL_PRODUCTS: Final = 10


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
        or parsed.hostname != _CCCENTER_HOST
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
        or parsed.hostname != _CCCENTER_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not re.fullmatch(r"/product/[a-z0-9]+(?:-[a-z0-9]+)*/", raw_path)
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl")
    return urlunsplit(("https", _CCCENTER_HOST, raw_path, "", ""))


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
        or parsed.hostname != _CCCENTER_HOST
        or parsed.port not in {None, 443}
    ):
        return None
    return urlunsplit(("https", _CCCENTER_HOST, parsed.path, parsed.query, ""))


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
    if not isinstance(image, Tag):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    image_url = _safe_image_url(
        str(image.get("src") or image.get("data-src") or image.get("srcset") or ""),
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
    listing: CCCenterListing | CCCenterProduct,
) -> CCCenterProduct:
    product_document = _main_product(document)
    heading = product_document.select_one("h1.product_title")
    price_nodes = product_document.select(".summary .price") or product_document.select(
        ".price",
    )
    if len(price_nodes) > 1:
        raise FeedFetchError(CCCENTER_LABEL, "AmbiguousPrice")
    price_container = price_nodes[0] if price_nodes else None
    current_price, original_price = _prices(price_container)
    is_variable = "product-type-variable" in (
        product_document.get("class") or []
    ) or bool(
        product_document.select("form.variations_form select[name^='attribute_']"),
    )
    price_status = _price_status(
        price_container,
        current_price,
        is_variable=is_variable,
    )
    sku = _text(product_document.select_one(".sku"))
    is_in_stock = _is_in_stock(product_document.select_one(".stock"))
    categories = tuple(
        category
        for category in (
            _text(node) for node in product_document.select(".posted_in a")
        )
        if category
    )
    image_url = listing.image_url
    for image in product_document.select(".woocommerce-product-gallery img"):
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


def _main_product(document: BeautifulSoup) -> Tag:
    # Work on a copy: callers may reuse the parsed document. Theme quick views,
    # related cards and empty sticky-cart forms do not describe the main product.
    cleaned = BeautifulSoup(str(document), _HTML_PARSER)
    for node in reversed(
        cleaned.select(
            ".related, .upsells, .cross-sells, .etheme-sticky-cart, aside, "
            ".etheme-quick-view, .quick-view, li.product, .etheme-product-grid-item",
        ),
    ):
        node.decompose()
    headings = cleaned.select("h1.product_title")
    if len(headings) != 1 or not _text(headings[0]):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    for node in reversed(cleaned.select(".product")):
        if node.select_one("h1.product_title") is None:
            node.decompose()
    for parent in headings[0].parents:
        if isinstance(parent, Tag) and (
            parent.name == "main"
            or set(parent.get("class") or []).intersection(
                {"product", "product-type-variable", "product-type-simple"},
            )
        ):
            return parent
    # Retain support for flat product fragments. Unknown nested page layouts
    # must not borrow a price from elsewhere in the document.
    container = headings[0].parent
    if isinstance(container, Tag) and container.name in {"[document]", "body"}:
        fragment = BeautifulSoup("", _HTML_PARSER)
        field_classes = {
            "product_title",
            "summary",
            "price",
            "sku",
            "stock",
            "posted_in",
            "woocommerce-product-gallery",
            "variations_form",
        }
        for child in list(container.children):
            if isinstance(child, Tag) and set(child.get("class") or []).intersection(
                field_classes,
            ):
                fragment.append(child.extract())
        return fragment
    raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")


def _is_in_stock(stock: Tag | None) -> bool:
    if stock is None:
        return True
    stock_classes = stock.get("class")
    return "out-of-stock" not in str(stock_classes or "")


def _pagination_page(value: str) -> int:
    """Accept only same-scope canonical path or legacy query pagination."""
    try:
        raw = urlsplit(value)
        parsed = urlsplit(urljoin(CCCENTER_FEED_URL, value))
        port = parsed.port
    except ValueError:
        raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination") from None
    if (
        not value
        or not value.startswith(("https://", "/", "?"))
        or parsed.scheme != "https"
        or parsed.hostname != _CCCENTER_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or (raw.path and raw.path != parsed.path)
    ):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    params = dict(query)
    if len(query) != len(params) or params.get("orderby") != "date":
        raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination")
    if parsed.path == CCCENTER_SHOP_PATH:
        if set(params) - {"orderby", "product-page"}:
            raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination")
        number = params.get("product-page", "1")
    else:
        match = re.fullmatch(r"/shop/page/([1-9][0-9]*)/", parsed.path)
        if match is None or set(params) != {"orderby"}:
            raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination")
        number = match[1]
    if not re.fullmatch(r"[1-9][0-9]{0,5}", number):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination")
    return int(number)


@dataclass(frozen=True, slots=True)
class _ResultRange:
    first: int
    last: int
    total: int


def _reported_range(document: BeautifulSoup) -> _ResultRange | None:
    """Read optional WooCommerce counts; unknown or conflicting markers fail."""
    ranges: set[_ResultRange] = set()
    for node in document.select(".woocommerce-result-count"):
        text = _text(node)
        match = re.fullmatch(
            r"\D*([0-9]{1,6})\s*[-–—]\s*([0-9]{1,6})\D+([0-9]{1,6})\D*",
            text,
        )
        if match is not None:
            first, last, total = (int(value) for value in match.groups())
        elif (single := re.fullmatch(r"\D*([0-9]{1,6})\D*", text)) is not None:
            first, last, total = 1, int(single[1]), int(single[1])
        elif text.casefold() == "showing the single result":
            first, last, total = 1, 1, 1
        else:
            raise FeedFetchError(CCCENTER_LABEL, "MalformedResultCount")
        if not 1 <= first <= last <= total:
            raise FeedFetchError(CCCENTER_LABEL, "MalformedResultCount")
        ranges.add(_ResultRange(first, last, total))
    if len(ranges) > 1:
        raise FeedFetchError(CCCENTER_LABEL, "ConflictingResultCount")
    return next(iter(ranges), None)


def _validate_cardinality(
    count: int,
    page: int,
    page_count: int,
    reported: _ResultRange | None,
) -> None:
    # Without trustworthy result counts, the observed 24-card contract is the
    # minimum completeness proof. Only the terminal page may be shorter.
    size = CCCENTER_PRODUCTS_PER_PAGE
    if not 1 <= count <= size or (page < page_count and count != size):
        raise FeedFetchError(CCCENTER_LABEL, "IncompletePage")
    if page_count == 1 and reported is None and count == size:
        raise FeedFetchError(CCCENTER_LABEL, "AmbiguousCatalogEnd")
    if reported is not None and (
        reported.first != (page - 1) * size + 1
        or reported.last != min(page * size, reported.total)
        or reported.last - reported.first + 1 != count
        or (reported.total + size - 1) // size != page_count
    ):
        raise FeedFetchError(CCCENTER_LABEL, "ResultCountMismatch")


class CCCenterCatalogClient:
    """Enumerate the complete bounded listing index without N+1 detail fetches."""

    def fetch_product_detail(
        self,
        listing: CCCenterListing | CCCenterProduct,
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> CCCenterProduct:
        """Optional single-product enrichment, separate from catalog enumeration."""
        return self.fetch_product_details(
            (listing,),
            is_shutdown_requested=is_shutdown_requested,
        )[0]

    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        """Fetch at most ten selected details under one aggregate scan budget.

        Validate all source identities before any transfer; preserve input order
        and return no partial batch on interruption or failure. There are no
        internal retries or fresh per-product deadlines. Callers must still
        compare scalar status, currency and price with the selected listing.
        """
        budget = _ScanBudget.start(is_shutdown_requested)
        budget.before_chunk()
        if len(listings) > MAX_CCCENTER_DETAIL_PRODUCTS:
            raise FeedFetchError(CCCENTER_LABEL, "DetailProductLimitExceeded")
        selected = tuple(listings)
        urls: list[str] = []
        for listing in selected:
            budget.before_chunk()
            url = _safe_product_url(listing.url)
            if listing.product_id != url:
                raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
            if url in urls:
                raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
            urls.append(url)
        products: list[CCCenterProduct] = []
        for listing, url in zip(selected, urls, strict=True):
            budget.before_chunk()
            html = self._fetch_html(url, budget=budget)
            product = parse_product_detail(BeautifulSoup(html, _HTML_PARSER), listing)
            budget.after_request()
            products.append(product)
        budget.after_request()
        return tuple(products)

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
        first_soup = BeautifulSoup(first_html, _HTML_PARSER)
        page_count = self._page_count(first_soup)
        initial_range = _reported_range(first_soup)
        products: list[CCCenterProduct] = []
        seen_ids: set[str] = set()
        for page in range(1, page_count + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            soup = self._page_document(page, first_soup, budget)
            if self._page_count(soup) != page_count:
                raise FeedFetchError(CCCENTER_LABEL, "PaginationDrift")
            current_pages = soup.select(".page-numbers.current")
            if (page_count > 1 and len(current_pages) != 1) or any(
                _text(node) != str(page) for node in current_pages
            ):
                raise FeedFetchError(CCCENTER_LABEL, "PaginationDrift")
            reported = _reported_range(soup)
            if (reported is None) != (initial_range is None) or (
                reported is not None
                and initial_range is not None
                and reported.total != initial_range.total
            ):
                raise FeedFetchError(CCCENTER_LABEL, "CatalogMetadataDrift")
            self._append_page_products(
                soup,
                products,
                seen_ids,
                budget,
                page=page,
                page_count=page_count,
                reported=reported,
            )
        budget.after_request()
        return tuple(products)

    def _page_document(
        self,
        page: int,
        first_soup: BeautifulSoup,
        budget: _ScanBudget,
    ) -> BeautifulSoup:
        if page == 1:
            return first_soup
        html = self._fetch_html(self._page_url(page), budget=budget)
        return BeautifulSoup(html, _HTML_PARSER)

    def _append_page_products(
        self,
        soup: BeautifulSoup,
        products: list[CCCenterProduct],
        seen_ids: set[str],
        budget: _ScanBudget,
        *,
        page: int,
        page_count: int,
        reported: _ResultRange | None,
    ) -> None:
        cards = soup.select("li.product, div.etheme-product-grid-item")
        if not cards:
            raise FeedFetchError(CCCENTER_LABEL, "EmptyPage")
        _validate_cardinality(len(cards), page, page_count, reported)
        for card in cards:
            budget.before_chunk()
            products.append(self._index_product(card, seen_ids))

    @staticmethod
    def _index_product(
        card: Tag,
        seen_ids: set[str],
    ) -> CCCenterProduct:
        listing = parse_product_listing(card)
        if listing.product_id in seen_ids:
            raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
        seen_ids.add(listing.product_id)
        if len(seen_ids) > MAX_CCCENTER_PRODUCTS:
            raise FeedFetchError(CCCENTER_LABEL, "ProductLimitExceeded")
        return CCCenterProduct(
            product_id=listing.product_id,
            name=listing.name,
            url=listing.url,
            sku=_text(card.select_one(".sku")),
            current_price=listing.current_price,
            original_price=listing.original_price,
            image_url=listing.image_url,
            categories=tuple(_text(node) for node in card.select(".posted_in a")),
            is_in_stock="outofstock" not in (card.get("class") or [])
            and _is_in_stock(card.select_one(".stock")),
            price_status=listing.price_status,
        )

    @staticmethod
    def _page_count(document: BeautifulSoup) -> int:
        links = document.select("a.page-numbers, .woocommerce-pagination a")
        current = document.select(".page-numbers.current")
        if document.select(".woocommerce-pagination") and not links and not current:
            raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination")
        pages = [_pagination_page(str(link.get("href", ""))) for link in links]
        for node in current:
            value = _text(node)
            if not re.fullmatch(r"[1-9][0-9]{0,5}", value):
                raise FeedFetchError(CCCENTER_LABEL, "MalformedPagination")
            pages.append(int(value))
        page_count = max(pages, default=1)
        if page_count > MAX_CCCENTER_PAGES:
            raise FeedFetchError(CCCENTER_LABEL, "PageLimitExceeded")
        return page_count

    @staticmethod
    def _page_url(page: int) -> str:
        if not 1 <= page <= MAX_CCCENTER_PAGES:
            raise FeedFetchError(CCCENTER_LABEL, "PageLimitExceeded")
        if page == 1:
            return CCCENTER_FEED_URL
        return f"{CCCENTER_ORIGIN}{CCCENTER_SHOP_PATH}page/{page}/?orderby=date"

    @staticmethod
    def _fetch_html(
        url: str,
        *,
        budget: _ScanBudget | None = None,
    ) -> str:
        if budget is not None:
            budget.before_request()
        callback_state = BoundedContentCallback.start(
            budget,
            max_bytes=MAX_CCCENTER_RESPONSE_BYTES,
            label=CCCENTER_LABEL,
        )
        response = CCCenterCatalogClient._request_html(url, budget, callback_state)
        _raise_callback_abort(callback_state)
        if budget is not None:
            budget.after_request()
        _validate_response(response)
        return _decode_html(response, callback_state.content)

    @staticmethod
    def _request_html(
        url: str,
        budget: _ScanBudget | None,
        callback_state: BoundedContentCallback,
    ) -> Any:  # noqa: ANN401
        try:
            timeout = budget.request_timeout() if budget is not None else 30.0
            return _perform_request(
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


def _raise_callback_abort(callback_state: BoundedContentCallback) -> None:
    if callback_state.abort_error is not None:
        raise callback_state.abort_error from None


def _decode_html(response: _HttpResponse, content: bytearray) -> str:
    try:
        return bytes(content).decode(response.encoding or "utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse") from None
