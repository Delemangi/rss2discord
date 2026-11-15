"""Parse CCCenter Store API products and WooCommerce product HTML."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any, Final
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import (
    PriceAmountValidationError,
    canonicalize_price_amount,
)
from rss2discord.providers.cccenter.bounds import (
    CCCENTER_HOST,
    CCCENTER_LABEL,
    CCCENTER_ORIGIN,
)
from rss2discord.providers.cccenter.models import (
    CCCenterListing,
    CCCenterPriceStatus,
    CCCenterProduct,
)

__all__ = [
    "parse_mkd_price",
    "parse_product_detail",
    "parse_product_listing",
]

_PRICE_RE: Final = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?(?!\d)",
)
_HTML_PARSER: Final = "html.parser"
_PRICE_SELECTOR: Final = ".price"
_PRODUCT_TITLE_SELECTOR: Final = "h1.product_title"
_ENCODED_DOUBLE_PRIME: Final = r"(?i:%e2%80%b3)"
_PRODUCT_SLUG_PART: Final = rf"(?:[a-z0-9]|{_ENCODED_DOUBLE_PRIME})+"
_PRODUCT_PATH_RE: Final = re.compile(
    rf"/product/{_PRODUCT_SLUG_PART}(?:-{_PRODUCT_SLUG_PART})*/",
)


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
        or parsed.hostname != CCCENTER_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidProductUrl")
    return urlunsplit(("https", CCCENTER_HOST, raw_path, "", ""))


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
        or parsed.hostname != CCCENTER_HOST
        or parsed.port not in {None, 443}
    ):
        return None
    return urlunsplit(("https", CCCENTER_HOST, parsed.path, parsed.query, ""))


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
