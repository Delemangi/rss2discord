"""Parse Wolfinet Odoo shop pages and product cards."""

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
from rss2discord.providers.wolfinet.bounds import (
    WOLFINET_HOST,
    WOLFINET_LABEL,
    WOLFINET_ORIGIN,
)
from rss2discord.providers.wolfinet.models import WolfinetProduct

__all__ = [
    "format_wolfinet_price",
    "parse_wolfinet_page",
    "parse_wolfinet_price",
    "parse_wolfinet_product_card",
]

_HTML_PARSER: Final = "html.parser"
_AMOUNT_RE: Final = re.compile(
    r"(?<!\d)(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?!\d)",
)
_CURRENCY_SUFFIX_RE: Final = re.compile(r"\s*(?:ден\.?|MKD\.?)\s*$", re.IGNORECASE)
_DISPLAY_PRICE_RE: Final = re.compile(
    r"(?<!\d)(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(?:ден\.?|MKD\.?)",
    re.IGNORECASE,
)
_PRODUCT_ID_RE: Final = re.compile(r"-(\d+)$")
_ALLOWED_HOSTS: Final = frozenset({WOLFINET_HOST, f"www.{WOLFINET_HOST}"})
_CARD_SELECTOR: Final = "div.o_wsale_product_grid_wrapper"
_LINK_SELECTOR: Final = "h6 a[itemprop='name']"
_IMAGE_SELECTOR: Final = "img[itemprop='image']"
_PRICE_VALUE_SELECTOR: Final = "span.oe_currency_value"
_PRICE_FALLBACK_SELECTOR: Final = ".product_price"
_DESCRIPTION_SELECTOR: Final = ".oe_subdescription"


def parse_wolfinet_price(value: str) -> Decimal | None:
    """Parse one bounded Wolfinet display amount, or return ``None``."""
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


def format_wolfinet_price(amount: Decimal) -> str:
    """Render one amount in the listing's comma-grouped denar style."""
    return f"{amount:,.2f} ден"


def _text(node: Tag | None) -> str:
    return " ".join(unescape(node.get_text(" ", strip=True)).split()) if node else ""


def _safe_product_url(value: str) -> tuple[str, str]:
    if not value or any(
        ord(character) <= 0x20 or ord(character) == 0x7F for character in value
    ):
        raise FeedFetchError(WOLFINET_LABEL, "InvalidProductUrl")
    try:
        absolute = urljoin(WOLFINET_ORIGIN + "/", value.strip())
        parsed = urlsplit(absolute)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(WOLFINET_LABEL, "InvalidProductUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FeedFetchError(WOLFINET_LABEL, "InvalidProductUrl")
    path = parsed.path.rstrip("/")
    if not path.startswith("/en/shop/"):
        raise FeedFetchError(WOLFINET_LABEL, "InvalidProductUrl")
    slug = path.removeprefix("/en/shop/")
    identity = _PRODUCT_ID_RE.search(slug)
    if "/" in slug or identity is None:
        raise FeedFetchError(WOLFINET_LABEL, "InvalidProductIdentity")
    return (
        urlunsplit(("https", WOLFINET_HOST, path, "", "")),
        identity.group(1),
    )


def _safe_image_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        absolute = urljoin(WOLFINET_ORIGIN + "/", value.strip())
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
    return urlunsplit(("https", WOLFINET_HOST, parsed.path, parsed.query, ""))


def _card_amount(card: Tag) -> Decimal | None:
    amount = parse_wolfinet_price(_text(card.select_one(_PRICE_VALUE_SELECTOR)))
    if amount is not None:
        return amount
    match = _DISPLAY_PRICE_RE.search(_text(card.select_one(_PRICE_FALLBACK_SELECTOR)))
    if match is None:
        return None
    return parse_wolfinet_price(match.group(0))


def parse_wolfinet_product_card(
    card: Tag | None,
    *,
    observed_at: datetime | None = None,
) -> WolfinetProduct:
    """Parse one complete product card, failing on identity or price problems."""
    if card is None:
        raise FeedFetchError(WOLFINET_LABEL, "MalformedProduct")
    link = card.select_one(_LINK_SELECTOR)
    if not isinstance(link, Tag):
        raise FeedFetchError(WOLFINET_LABEL, "MalformedProduct")
    name = _text(link)
    if not name:
        raise FeedFetchError(WOLFINET_LABEL, "MalformedProduct")
    url, product_id = _safe_product_url(str(link.get("href") or ""))
    amount = _card_amount(card)
    if amount is None or amount <= 0:
        raise FeedFetchError(WOLFINET_LABEL, "InvalidPrice")
    image = card.select_one(_IMAGE_SELECTOR)
    image_url = (
        _safe_image_url(str(image.get("src") or "")) if isinstance(image, Tag) else None
    )
    return WolfinetProduct(
        product_id=product_id,
        name=name,
        url=url,
        image_url=image_url,
        description=_text(card.select_one(_DESCRIPTION_SELECTOR)),
        amount=amount,
        formatted_price=format_wolfinet_price(amount),
        currency="MKD",
        observed_at=observed_at or datetime.now(UTC),
    )


def parse_wolfinet_page(
    html: str,
    *,
    observed_at: datetime | None = None,
) -> tuple[WolfinetProduct, ...]:
    """Parse one shop page, skipping malformed or third-party cards."""
    document = BeautifulSoup(html, _HTML_PARSER)
    observed_at = observed_at or datetime.now(UTC)
    products: list[WolfinetProduct] = []
    for card in document.select(_CARD_SELECTOR):
        try:
            products.append(
                parse_wolfinet_product_card(card, observed_at=observed_at),
            )
        except FeedFetchError:
            continue
    return tuple(products)
