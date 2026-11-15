"""Parse Technomarket category pages, product cards and MKD prices."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import (
    PriceAmountValidationError,
    canonicalize_price_amount,
)
from rss2discord.providers.technomarket.bounds import (
    MAX_TECHNOMARKET_PAGES,
    TECHNOMARKET_HOST,
    TECHNOMARKET_LABEL,
    TECHNOMARKET_ORIGIN,
)
from rss2discord.providers.technomarket.models import TechnomarketProduct

__all__ = [
    "parse_mkd_price",
    "parse_product_card",
]

_HTML_PARSER: Final = "html.parser"
_PRICE_RE: Final = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[.\s,]\d{3})+|\d+)(?:[.,]\d{1,2})?(?!\d)",
)
_CATEGORY_PAGE_RE: Final = re.compile(
    r"^/category/\d+/[a-z0-9]+(?:-[a-z0-9]+)*/page/(\d+)/?$",
)
_PRODUCT_PATH_RE: Final = re.compile(r"^/(?:product|products)/[^?#]+/?$")
_PRODUCT_RANGE_RE: Final = re.compile(
    r"^(\d+)\s*-\s*(\d+)\s+од\s+(\d+)\s+производи$",
    re.IGNORECASE,
)


def parse_mkd_price(value: str) -> Decimal | None:
    """Parse one bounded MKD amount from a listing price label."""
    text = " ".join(unescape(value).replace("\xa0", " ").split())
    match = _PRICE_RE.search(text)
    if match is None:
        return None
    normalized = match.group(0).replace(" ", "")
    if "," in normalized:
        comma_parts = normalized.split(",")
        if len(comma_parts) == 2 and len(comma_parts[1]) == 3:
            normalized = "".join(comma_parts)
        elif len(comma_parts) == 2:
            whole, fraction = comma_parts
            normalized = f"{whole.replace('.', '')}.{fraction}"
        else:
            normalized = "".join(comma_parts)
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
    try:
        absolute = urljoin(TECHNOMARKET_ORIGIN + "/", value.strip())
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidProductUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != TECHNOMARKET_HOST
        or port is not None
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
    try:
        absolute = urljoin(TECHNOMARKET_ORIGIN + "/", value.split(",", 1)[0].strip())
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        return None
    return urlunsplit(("https", parsed.hostname or "", parsed.path, parsed.query, ""))


def parse_product_card(
    card: Tag | None,
    *,
    observed_at: datetime | None = None,
) -> TechnomarketProduct:
    """Parse a complete product card, failing closed on identity errors."""
    if card is None:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedProduct")
    link, title = _required_card_nodes(card)
    url, product_id = _safe_product_url(str(link.get("href", "")))
    _validate_product_identity(card, product_id)
    regular_node, smart_node = _price_nodes(card)
    image = _first(
        card,
        (
            "img.product-image",
            "img[data-src]",
            "img[data-original]",
            "img[src]",
            "figure.product-figure",
        ),
    )
    return TechnomarketProduct(
        product_id=product_id,
        name=_text(title),
        url=url,
        image_url=_safe_image_url(_image_value(image) if image else None),
        manufacturer=_manufacturer(card),
        categories=_categories(card),
        regular_price=_price(regular_node),
        smart_price=_price(smart_node),
        observed_at=observed_at or datetime.now(UTC),
    )


def _required_card_nodes(card: Tag) -> tuple[Tag, Tag]:
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
    if link is None or title is None or not _text(title):
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedProduct")
    return link, title


def _validate_product_identity(card: Tag, product_id: str) -> None:
    data_id = str(card.get("data-id") or "")
    if not data_id.isdigit() or data_id != product_id:
        raise FeedFetchError(TECHNOMARKET_LABEL, "InvalidProductIdentity")


def _price_nodes(card: Tag) -> tuple[Tag | None, Tag | None]:
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
    return regular_node, smart_node


def _manufacturer(card: Tag) -> str | None:
    manufacturer = (
        _text(
            _first(card, ("[data-manufacturer]", ".manufacturer", ".brand")),
        )
        or None
    )
    if manufacturer is not None:
        return manufacturer
    for node in card.select(".product-price > div, .product-price div"):
        if "Производител" in _text(node):
            manufacturer = _text(node.select_one("strong")) or None
            if manufacturer:
                return manufacturer
    return None


def _categories(card: Tag) -> tuple[str, ...]:
    category_nodes = card.select(
        "[data-category], .category, .product-category, .categories a",
    )
    return tuple(dict.fromkeys(_text(node) for node in category_nodes if _text(node)))


def _image_value(image: Tag) -> str:
    if image.name == "figure":
        match = re.search(r"url\(['\"]?([^)'\"]+)", str(image.get("style") or ""))
        return match.group(1) if match else ""
    return str(
        image.get("data-src") or image.get("data-original") or image.get("src") or "",
    )


@dataclass(frozen=True, slots=True)
class _PageInfo:
    first: int
    last: int
    total: int


def _page_info(document: BeautifulSoup) -> _PageInfo:
    ranges = document.select(".products-range")
    if not ranges:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedCount")
    parsed_ranges: list[_PageInfo] = []
    for node in ranges:
        match = _PRODUCT_RANGE_RE.fullmatch(_text(node))
        if match is None:
            raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedCount")
        first, last, total = (int(value) for value in match.groups())
        if first < 1 or last < first or total < last:
            raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedCount")
        parsed_ranges.append(_PageInfo(first, last, total))
    if len(set(parsed_ranges)) != 1:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedCount")
    return parsed_ranges[0]


def _page_count(document: BeautifulSoup) -> int:
    page_values = (
        _page_number(link.get("href")) for link in document.select("a[href]")
    )
    count = max((page for page in page_values if page is not None), default=1)
    if count < 1:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedPagination")
    if count > MAX_TECHNOMARKET_PAGES:
        raise FeedFetchError(TECHNOMARKET_LABEL, "PageLimitExceeded")
    return count


def _page_number(value: object) -> int | None:
    href = str(value or "").strip()
    if not href or href == "#":
        return None
    try:
        parsed = urlsplit(urljoin(TECHNOMARKET_ORIGIN + "/", href))
        port = parsed.port
    except ValueError:
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedPagination") from None
    match = _CATEGORY_PAGE_RE.fullmatch(parsed.path)
    if match is None:
        if "/page/" in parsed.path:
            raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedPagination")
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != TECHNOMARKET_HOST
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(TECHNOMARKET_LABEL, "MalformedPagination")
    return int(match.group(1))


def _total_products(document: BeautifulSoup) -> int:
    return _page_info(document).total
