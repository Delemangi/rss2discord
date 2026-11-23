"""Parse PROXNet PrestaShop category pages and product cards."""

from __future__ import annotations

import re
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
from rss2discord.providers.proxnet.bounds import (
    PROXNET_HOST,
    PROXNET_LABEL,
    PROXNET_ORIGIN,
)
from rss2discord.providers.proxnet.models import ProxnetPage, ProxnetProduct

__all__ = [
    "parse_mkd_price",
    "parse_proxnet_page",
    "parse_proxnet_product_card",
]

_HTML_PARSER: Final = "html.parser"
_AMOUNT_RE: Final = re.compile(
    r"(?<!\d)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?!\d)",
)
_CURRENCY_SUFFIX_RE: Final = re.compile(r"\s*(?:ден\.?|MKD\.?)\s*$", re.IGNORECASE)
_NUMERIC_GROUP_RE: Final = re.compile(r"(?<!\d)(\d+)(?!\d)")
_ALLOWED_HOSTS: Final = frozenset({"www.proxnet.mk", "proxnet.mk"})
_PAGE_TITLE_SELECTORS: Final = (
    ".cat-name",
    "h1.page-heading",
    "h1[itemprop='name']",
    "h1",
    "title",
)
_CARD_SELECTOR: Final = "li.ajax_block_product"
_LINK_SELECTOR: Final = "a.product-name[href]"
_IMAGE_SELECTOR: Final = "a.product_img_link img[src]"
_PRICE_SELECTOR: Final = "span.price.product-price"
_DESCRIPTION_SELECTOR: Final = "p.product-desc"


def parse_mkd_price(value: str) -> Decimal | None:
    """Parse one bounded Macedonian denar display amount, or return ``None``."""
    text = " ".join(unescape(value).replace("\xa0", " ").split())
    text = _CURRENCY_SUFFIX_RE.sub("", text).strip()
    match = _AMOUNT_RE.fullmatch(text)
    if match is None:
        return None
    normalized = match.group(1).replace(",", "")
    try:
        amount = Decimal(normalized)
        canonicalize_price_amount(amount)
    except (InvalidOperation, PriceAmountValidationError):
        return None
    return amount if amount > 0 else None


def _text(node: Tag | None) -> str:
    return " ".join(unescape(node.get_text(" ", strip=True)).split()) if node else ""


def _safe_product_url(value: str) -> tuple[str, str]:
    if not value or any(
        ord(character) <= 0x20 or ord(character) == 0x7F for character in value
    ):
        raise FeedFetchError(PROXNET_LABEL, "InvalidProductUrl")
    try:
        absolute = urljoin(PROXNET_ORIGIN + "/", value.strip())
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(PROXNET_LABEL, "InvalidProductUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(".html")
    ):
        raise FeedFetchError(PROXNET_LABEL, "InvalidProductUrl")
    identity = _NUMERIC_GROUP_RE.search(parsed.path.rsplit("/", 1)[-1])
    if identity is None:
        raise FeedFetchError(PROXNET_LABEL, "InvalidProductIdentity")
    return (
        urlunsplit(("https", PROXNET_HOST, parsed.path, "", "")),
        identity.group(1),
    )


def _safe_image_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        absolute = urljoin(PROXNET_ORIGIN + "/", value.strip())
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    return urlunsplit(("https", PROXNET_HOST, parsed.path, parsed.query, ""))


def _page_title(document: BeautifulSoup) -> str:
    for selector in _PAGE_TITLE_SELECTORS:
        node = document.select_one(selector)
        title = _text(node)
        if title:
            return title
    return ""


def parse_proxnet_product_card(
    card: Tag | None,
    *,
    category: str = "",
    observed_at: datetime | None = None,
) -> ProxnetProduct:
    """Parse one complete product card, failing on identity or price problems."""
    if card is None:
        raise FeedFetchError(PROXNET_LABEL, "MalformedProduct")
    link = card.select_one(_LINK_SELECTOR)
    if not isinstance(link, Tag):
        raise FeedFetchError(PROXNET_LABEL, "MalformedProduct")
    name = " ".join(
        unescape(str(link.get("title") or "")).split(),
    ) or _text(link)
    if not name:
        raise FeedFetchError(PROXNET_LABEL, "MalformedProduct")
    url, product_id = _safe_product_url(str(link.get("href", "")))
    price_node = card.select_one(_PRICE_SELECTOR)
    formatted_price = _text(price_node)
    amount = parse_mkd_price(formatted_price)
    if amount is None or amount <= 0:
        raise FeedFetchError(PROXNET_LABEL, "InvalidPrice")
    image = card.select_one(_IMAGE_SELECTOR)
    image_url = (
        _safe_image_url(str(image.get("src", ""))) if isinstance(image, Tag) else None
    )
    return ProxnetProduct(
        product_id=product_id,
        name=name,
        url=url,
        image_url=image_url,
        description=_text(card.select_one(_DESCRIPTION_SELECTOR)),
        amount=amount,
        formatted_price=formatted_price,
        currency="MKD",
        observed_at=observed_at or datetime.now(UTC),
        category=category,
    )


def parse_proxnet_page(
    html: str,
    *,
    observed_at: datetime | None = None,
) -> ProxnetPage:
    """Parse one category page, skipping malformed or third-party cards."""
    document = BeautifulSoup(html, _HTML_PARSER)
    title = _page_title(document)
    observed_at = observed_at or datetime.now(UTC)
    products: list[ProxnetProduct] = []
    for card in document.select(_CARD_SELECTOR):
        try:
            products.append(
                parse_proxnet_product_card(
                    card,
                    category=title,
                    observed_at=observed_at,
                ),
            )
        except FeedFetchError:
            continue
    return ProxnetPage(products=tuple(products), title=title)
