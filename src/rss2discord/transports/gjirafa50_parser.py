"""Parse Gjirafa50 catalog JSON and product-card HTML."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, ClassVar, Final
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.gjirafa50_models import (
    Gjirafa50CatalogPage,
    Gjirafa50Product,
)

GJIRAFA50_LABEL: Final = "Gjirafa50"
GJIRAFA50_ORIGIN: Final = "https://gjirafa50.mk"
GJIRAFA50_IMAGE_HOST: Final = "50cdn.gjirafamall.tech"
MAX_GJIRAFA50_TEXT_LENGTH: Final = 500
MAX_GJIRAFA50_URL_LENGTH: Final = 2_048


class _CatalogEnvelope(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    total_pages: Annotated[int, Field(alias="totalpages", ge=0)]
    total_hits: Annotated[int, Field(alias="totalHits", ge=0)]
    products_count: Annotated[int, Field(alias="productsCount", ge=0)]
    html: str


def parse_gjirafa50_page(
    content: bytes,
    observed_at: datetime,
) -> Gjirafa50CatalogPage:
    try:
        envelope = _CatalogEnvelope.model_validate_json(content)
    except ValidationError:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidResponse") from None
    soup = BeautifulSoup(envelope.html, "html.parser")
    products = tuple(_parse_product(card, observed_at) for card in soup.select(".product-item"))
    if len(products) != envelope.products_count:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidCardinality")
    return Gjirafa50CatalogPage(envelope.total_hits, envelope.total_pages, products)


def _parse_product(card: Tag, observed_at: datetime) -> Gjirafa50Product:
    product_id = _positive_int(_attribute(card, "data-productid"))
    price = _price(_attribute(card, "data-discountedprice"))
    title_link = card.select_one(".product-title a[href]")
    if title_link is None:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    title = title_link.get_text(" ", strip=True)
    href = _attribute(title_link, "href")
    if not title or len(title) > MAX_GJIRAFA50_TEXT_LENGTH:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    link = _product_url(href)
    image = card.select_one(".picture img[src]")
    image_url = _image_url(_attribute(image, "src")) if image is not None else None
    major, minor = f"{price:,.2f}".split(".")
    formatted = f"{major.replace(',', '.')},{minor} MKD."
    return Gjirafa50Product(
        product_id,
        title,
        link,
        image_url,
        price,
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
    if value is None:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    try:
        amount = Decimal(value.replace(".", "").replace(",", "."))
        canonicalize_price_amount(amount)
    except (InvalidOperation, ValueError):
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct") from None
    if amount < 0 or amount != amount.to_integral_value():
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProduct")
    return amount


def _product_url(href: str) -> str:
    url = urljoin(f"{GJIRAFA50_ORIGIN}/", href)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(GJIRAFA50_LABEL, "InvalidProductUrl") from None
    if (
        len(url) > MAX_GJIRAFA50_URL_LENGTH
        or parsed.scheme != "https"
        or parsed.hostname != "gjirafa50.mk"
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
