"""Bounded CCCenter WooCommerce Store API catalog scraping."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import unescape
from time import monotonic
from typing import Any, Final, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as curl_requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import (
    PriceAmountValidationError,
    canonicalize_price_amount,
)
from rss2discord.providers.cccenter.bounds import (
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
from rss2discord.providers.cccenter.models import (
    CCCenterListing,
    CCCenterPriceStatus,
    CCCenterProduct,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.catalog_http import BoundedContentCallback

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
_PRICE_SELECTOR: Final = ".price"
_PRODUCT_TITLE_SELECTOR: Final = "h1.product_title"
MAX_CCCENTER_DETAIL_PRODUCTS: Final = 10
_ENCODED_DOUBLE_PRIME: Final = r"(?i:%e2%80%b3)"
_PRODUCT_SLUG_PART: Final = rf"(?:[a-z0-9]|{_ENCODED_DOUBLE_PRIME})+"
_PRODUCT_PATH_RE: Final = re.compile(
    rf"/product/{_PRODUCT_SLUG_PART}(?:-{_PRODUCT_SLUG_PART})*/",
)
_STORE_API: Final = f"{CCCENTER_ORIGIN}/?rest_route=/wc/store/v1/products"


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
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in url):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl")
    try:
        raw = urlsplit(url)
    except ValueError:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl") from None
    raw_path = raw.path
    if _PRODUCT_PATH_RE.fullmatch(raw_path) is None:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl")
    try:
        absolute = urljoin(CCCENTER_ORIGIN + "/", url)
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl") from None
    if not raw.scheme and not raw.netloc and not url.startswith("/product/"):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl")
    if (
        parsed.scheme != "https"
        or parsed.hostname != _CCCENTER_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
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
    current_price, original_price = _prices(card.select_one(_PRICE_SELECTOR))
    price_status = _price_status(
        card.select_one(_PRICE_SELECTOR),
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
    heading = product_document.select_one(_PRODUCT_TITLE_SELECTOR)
    price_nodes = product_document.select(".summary .price") or product_document.select(
        _PRICE_SELECTOR,
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
    if isinstance(listing, CCCenterProduct):
        is_in_stock = listing.is_in_stock and is_in_stock
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
    headings = cleaned.select(_PRODUCT_TITLE_SELECTOR)
    if len(headings) != 1 or not _text(headings[0]):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    for node in reversed(cleaned.select(".product")):
        if node.select_one(_PRODUCT_TITLE_SELECTOR) is None:
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
        return _product_fragment(container)
    raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")


def _product_fragment(container: Tag) -> BeautifulSoup:
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
    # Collect selected children before extraction mutates the source container.
    fields = [
        child
        for child in container.children
        if isinstance(child, Tag)
        and set(child.get("class") or []).intersection(field_classes)
    ]
    for child in fields:
        fragment.append(child.extract())
    return fragment


def _is_in_stock(stock: Tag | None) -> bool:
    if stock is None:
        return True
    stock_classes = stock.get("class")
    return "out-of-stock" not in str(stock_classes or "")


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
        api_ids: set[int] = set()
        for listing in selected:
            budget.before_chunk()
            url = _safe_product_url(listing.url)
            if listing.product_id != url:
                raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
            locator = listing.store_api_id
            if (
                isinstance(locator, bool)
                or not isinstance(locator, int)
                or locator <= 0
            ):
                raise FeedFetchError(CCCENTER_LABEL, "InvalidProductLocator")
            if url in urls:
                raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
            if locator in api_ids:
                raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
            urls.append(url)
            api_ids.add(locator)
        products: list[CCCenterProduct] = []
        for listing, url in zip(selected, urls, strict=True):
            budget.before_chunk()
            product = self._fetch_store_product(url, listing, budget)
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
        validate_cccenter_url(url)
        first, total, page_count = self._fetch_store_page(1, budget)
        if total > MAX_CCCENTER_PRODUCTS:
            raise FeedFetchError(CCCENTER_LABEL, "ProductLimitExceeded")
        if page_count > MAX_CCCENTER_PAGES:
            raise FeedFetchError(CCCENTER_LABEL, "PageLimitExceeded")
        products: list[CCCenterProduct] = []
        seen_ids: set[tuple[str, str | int]] = set()
        for page in range(1, page_count + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if page == 1:
                cards = first
            else:
                # Fetch metadata alongside cards and require a stable collection.
                cards, page_total, page_pages = self._fetch_store_page(page, budget)
                if page_total != total or page_pages != page_count:
                    raise FeedFetchError(CCCENTER_LABEL, "CatalogMetadataDrift")
            expected = min(
                CCCENTER_PRODUCTS_PER_PAGE,
                total - (page - 1) * CCCENTER_PRODUCTS_PER_PAGE,
            )
            if len(cards) != expected:
                raise FeedFetchError(CCCENTER_LABEL, "IncompletePage")
            self._append_store_products(cards, products, seen_ids, budget)
        # Recheck first-page metadata after enumeration to detect collection drift.
        self._check_final_page(total, page_count, budget)
        budget.after_request()
        return tuple(products)

    def _check_final_page(
        self,
        total: int,
        page_count: int,
        budget: _ScanBudget,
    ) -> None:
        final_cards, final_total, final_pages = self._fetch_store_page(1, budget)
        if final_total != total or final_pages != page_count:
            raise FeedFetchError(CCCENTER_LABEL, "CatalogMetadataDrift")
        if len(final_cards) != min(total, CCCENTER_PRODUCTS_PER_PAGE):
            raise FeedFetchError(CCCENTER_LABEL, "IncompletePage")
        final_seen: set[tuple[str, str | int]] = set()
        for item in final_cards:
            self._store_product(item, final_seen)

    def _append_store_products(
        self,
        cards: Sequence[Any],
        products: list[CCCenterProduct],
        seen_ids: set[tuple[str, str | int]],
        budget: _ScanBudget,
    ) -> None:
        for item in cards:
            budget.before_chunk()
            products.append(self._store_product(item, seen_ids))

    @staticmethod
    def _store_product(
        item: Any,  # noqa: ANN401
        seen_ids: set[tuple[str, str | int]],
    ) -> CCCenterProduct:
        if not isinstance(item, dict):
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        slug = item.get("slug")
        permalink = item.get("permalink")
        if not isinstance(slug, str) or not slug or not isinstance(permalink, str):
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        url = _safe_product_url(permalink)
        path_slug = urlsplit(url).path.removeprefix("/product/").removesuffix("/")
        if unquote(path_slug) != unquote(slug):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
        id_key: tuple[str, str | int] = ("id", raw_id)
        url_key: tuple[str, str | int] = ("url", url)
        if id_key in seen_ids or url_key in seen_ids:
            raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
        seen_ids.update((id_key, url_key))
        name = item.get("name")
        if not isinstance(name, str) or not unescape(name).strip():
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        current, original, status = _store_price(item)
        image, sku, categories = _store_display_fields(item)
        stock = item.get("is_in_stock")
        if not isinstance(stock, bool):
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        return CCCenterProduct(
            url,
            unescape(name).strip(),
            url,
            sku,
            current,
            original,
            _safe_image_url(image if isinstance(image, str) else None),
            categories,
            stock,
            price_status=status,
            store_api_id=raw_id,
        )

    @staticmethod
    def _fetch_store_page(page: int, budget: _ScanBudget) -> tuple[list[Any], int, int]:
        query = urlencode(
            {
                "per_page": CCCENTER_PRODUCTS_PER_PAGE,
                "page": page,
                "orderby": "date",
                "order": "desc",
            },
        )
        response, content = CCCenterCatalogClient._fetch_json(
            f"{_STORE_API}&{query}",
            budget,
        )
        try:
            items = json.loads(content)
            total, pages = (
                int(response.headers["X-WP-Total"]),
                int(response.headers["X-WP-TotalPages"]),
            )
        except (ValueError, KeyError, TypeError):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse") from None
        if not isinstance(items, list) or total <= 0 or pages <= 0:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse")
        if total > MAX_CCCENTER_PRODUCTS:
            raise FeedFetchError(CCCENTER_LABEL, "ProductLimitExceeded")
        if pages > MAX_CCCENTER_PAGES:
            raise FeedFetchError(CCCENTER_LABEL, "PageLimitExceeded")
        if (
            pages
            != (total + CCCENTER_PRODUCTS_PER_PAGE - 1) // CCCENTER_PRODUCTS_PER_PAGE
            or page > pages
        ):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse")
        return items, total, pages

    @staticmethod
    def _fetch_store_product(
        url: str,
        listing: CCCenterListing | CCCenterProduct,
        budget: _ScanBudget,
    ) -> CCCenterProduct:
        query = urlencode({"include": listing.store_api_id, "per_page": 1, "page": 1})
        response, content = CCCenterCatalogClient._fetch_json(
            f"{_STORE_API}&{query}",
            budget,
        )
        try:
            items = json.loads(content)
            total = int(response.headers["X-WP-Total"])
            pages = int(response.headers["X-WP-TotalPages"])
        except (ValueError, KeyError, TypeError):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidDetail") from None
        if total != 1 or pages != 1:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidDetail")
        if not isinstance(items, list) or len(items) != 1:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidDetail")
        detail = CCCenterCatalogClient._store_product(items[0], set())
        if detail.store_api_id != listing.store_api_id or detail.url != url:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
        return CCCenterProduct(
            listing.product_id,
            detail.name,
            listing.url,
            detail.sku,
            detail.current_price,
            detail.original_price,
            detail.image_url or listing.image_url,
            detail.categories,
            detail.is_in_stock,
            price_status=_merge_price_status(listing.price_status, detail.price_status),
            store_api_id=detail.store_api_id,
        )

    @staticmethod
    def _fetch_json(url: str, budget: _ScanBudget) -> tuple[Any, str]:
        budget.before_request()
        callback = BoundedContentCallback.start(
            budget,
            max_bytes=MAX_CCCENTER_RESPONSE_BYTES,
            label=CCCENTER_LABEL,
        )
        response = CCCenterCatalogClient._request_json(url, budget, callback)
        _raise_callback_abort(callback)
        budget.after_request()
        _validate_response(response)
        content_type = (
            response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse")
        return response, _decode_html(response, callback.content)

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

    @staticmethod
    def _request_json(
        url: str,
        budget: _ScanBudget,
        callback_state: BoundedContentCallback,
    ) -> Any:  # noqa: ANN401
        try:
            return _perform_request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": CCCENTER_USER_AGENT,
                },
                timeout=budget.request_timeout(),
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


def _store_display_fields(
    item: dict[str, Any],
) -> tuple[Any, str, tuple[str, ...]]:
    images = item.get("images", [])
    if not isinstance(images, list):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    image = images[0].get("src") if images and isinstance(images[0], dict) else None
    sku = item.get("sku", "")
    cats = item.get("categories", [])
    if not isinstance(sku, str) or not isinstance(cats, list):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    if not all(
        isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip()
        for c in cats
    ):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    return image, sku, tuple(unescape(c["name"]).strip() for c in cats)


def _store_price(
    item: dict[str, Any],
) -> tuple[Decimal | None, Decimal | None, CCCenterPriceStatus]:
    product_type = item.get("type")
    has_options = item.get("has_options")
    if not isinstance(product_type, str) or product_type not in {
        "simple",
        "variable",
        "grouped",
        "external",
        "woosb",
    }:
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    if not isinstance(has_options, bool):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
    prices = item.get("prices")
    if not isinstance(prices, dict):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedPrice")
    if (
        prices.get("currency_code") != "MKD"
        or type(prices.get("currency_minor_unit")) is not int
        or prices["currency_minor_unit"] != 2
    ):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidCurrency")
    if product_type in {"variable", "woosb"} or has_options:
        return None, None, "variable"
    if prices.get("price_range") is not None:
        return None, None, "range"
    raw = prices.get("price")
    if raw in (None, ""):
        return None, None, "unpriced"
    current = _minor_amount(raw)
    original_raw = prices.get("regular_price")
    original = (
        _minor_amount(original_raw) if original_raw not in (None, "", raw) else None
    )
    return current, original, "scalar"


def _minor_amount(raw: Any) -> Decimal:  # noqa: ANN401
    if not isinstance(raw, str) or not re.fullmatch(r"\d+", raw, flags=re.ASCII):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedPrice")
    try:
        whole = raw[:-2] or "0"
        amount = Decimal(f"{whole}.{raw[-2:].zfill(2)}")
        canonicalize_price_amount(amount)
    except (InvalidOperation, PriceAmountValidationError):
        raise FeedFetchError(CCCENTER_LABEL, "MalformedPrice") from None
    return amount
