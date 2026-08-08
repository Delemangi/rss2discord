"""Parse Gjirafa50 catalog JSON and product-card HTML."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, ClassVar, Final
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rss2discord.price_amount import (
    MAX_PRICE_AMOUNT_WHOLE_DIGITS,
    canonicalize_price_amount,
)
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.gjirafa50_models import (
    Gjirafa50CatalogPage,
    Gjirafa50Product,
)

GJIRAFA50_LABEL: Final = "Gjirafa50"
GJIRAFA50_IMAGE_HOST: Final = "50cdn.gjirafamall.tech"
MAX_GJIRAFA50_TEXT_LENGTH: Final = 500
MAX_GJIRAFA50_URL_LENGTH: Final = 2_048
MAX_GJIRAFA50_PAGE_PRODUCTS: Final = 24
MAX_GJIRAFA50_HTML_TAG_TOKENS: Final = 10_000
MAX_GJIRAFA50_PRICE_TEXT_LENGTH: Final = 20
MAX_GJIRAFA50_PRICE_THOUSANDS_GROUPS: Final = (MAX_PRICE_AMOUNT_WHOLE_DIGITS - 1) // 3
GJIRAFA50_PRICE_PATTERN: Final = re.compile(
    rf"(?:0|[1-9][0-9]{{0,{MAX_PRICE_AMOUNT_WHOLE_DIGITS - 1}}}|"
    rf"[1-9][0-9]{{0,2}}(?:\.[0-9]{{3}})"
    rf"{{1,{MAX_GJIRAFA50_PRICE_THOUSANDS_GROUPS}}}),[0-9]{{2,4}}",
)


class _CatalogEnvelope(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    total_pages: Annotated[int, Field(alias="totalpages", ge=0)]
    total_hits: Annotated[int, Field(alias="totalHits", ge=0)]
    products_count: Annotated[
        int,
        Field(alias="productsCount", ge=0, le=MAX_GJIRAFA50_PAGE_PRODUCTS),
    ]
    html: str


def parse_gjirafa50_page(
    content: bytes,
    observed_at: datetime,
    root_url: str,
) -> Gjirafa50CatalogPage:
    try:
        envelope = _CatalogEnvelope.model_validate_json(content)
    except ValidationError:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidResponse") from None
    if envelope.html.count("<") > MAX_GJIRAFA50_HTML_TAG_TOKENS:
        raise FeedFetchError(GJIRAFA50_LABEL, "HtmlComplexityExceeded")
    soup = BeautifulSoup(envelope.html, "html.parser")
    cards = soup.select(".product-item")
    if len(cards) != envelope.products_count:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidCardinality")
    products = tuple(_parse_product(card, observed_at, root_url) for card in cards)
    return Gjirafa50CatalogPage(envelope.total_hits, envelope.total_pages, products)


def _parse_product(
    card: Tag,
    observed_at: datetime,
    root_url: str,
) -> Gjirafa50Product:
    product_id = _positive_int(_attribute(card, "data-productid"))
    price = _price(_attribute(card, "data-discountedprice"))
    title_link = card.select_one(".product-title a[href]")
    if title_link is None:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    title = title_link.get_text(" ", strip=True)
    href = _attribute(title_link, "href")
    if not title or len(title) > MAX_GJIRAFA50_TEXT_LENGTH:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    link = _product_url(href, root_url)
    image = card.select_one(".picture img[src]")
    image_url = _image_url(_attribute(image, "src")) if image is not None else None
    match urlsplit(root_url).hostname:
        case "gjirafa50.com":
            currency = "EUR"
            formatted = f"{price:,.2f} €"
        case "gjirafa50.mk":
            currency = "MKD"
            major, minor = f"{price:,.2f}".split(".")
            formatted = f"{major.replace(',', '.')},{minor} MKD."
        case _:
            raise FeedFetchError(GJIRAFA50_LABEL, "InvalidUrl")
    return Gjirafa50Product(
        product_id,
        title,
        link,
        image_url,
        price,
        currency,
        formatted,
        observed_at,
    )


def _attribute(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if not isinstance(value, str):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    return value


def _positive_int(value: str | None) -> int:
    if value is None or not value.isascii() or not value.isdigit():
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    parsed = int(value)
    if parsed <= 0:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    return parsed


def _price(value: str | None) -> Decimal:
    if (
        value is None
        or len(value) > MAX_GJIRAFA50_PRICE_TEXT_LENGTH
        or GJIRAFA50_PRICE_PATTERN.fullmatch(value) is None
    ):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    try:
        amount = Decimal(value.replace(".", "").replace(",", "."))
        canonicalize_price_amount(amount)
        quantized = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct") from None
    if amount != quantized:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    return amount


def _product_url(href: str, root_url: str) -> str:
    url = urljoin(root_url, href)
    expected_host = urlsplit(root_url).hostname
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProductUrl") from None
    if (
        len(url) > MAX_GJIRAFA50_URL_LENGTH
        or parsed.scheme != "https"
        or parsed.hostname != expected_host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProductUrl")
    return url


def _image_url(value: str | None) -> str:
    if value is None:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidImageUrl")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidImageUrl") from None
    if (
        len(value) > MAX_GJIRAFA50_URL_LENGTH
        or parsed.scheme != "https"
        or parsed.hostname != GJIRAFA50_IMAGE_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidImageUrl")
    return value
