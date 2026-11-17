"""Validated WooCommerce Store API models shared by every shop."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import unescape
from typing import Annotated, ClassVar, Final, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from rss2discord.price_amount import canonicalize_price_amount

MAX_WOOCOMMERCE_CATEGORIES_PER_PRODUCT: Final = 16
MAX_WOOCOMMERCE_IMAGES_PER_PRODUCT: Final = 16


class WooCommercePrices(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    price: Annotated[str, Field(pattern=r"^\d+$", max_length=24)]
    regular_price: Annotated[str, Field(pattern=r"^\d*$", max_length=24)] = ""
    currency_code: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    currency_minor_unit: Annotated[int, Field(ge=0, le=4)]

    @model_validator(mode="after")
    def require_persistable_prices(self) -> Self:
        current = self.current_amount
        regular = self.regular_amount
        canonicalize_price_amount(current)
        canonicalize_price_amount(regular)
        if regular < current:
            msg = "regular price cannot be lower than current price"
            raise ValueError(msg)
        return self

    @property
    def current_amount(self) -> Decimal:
        return Decimal(self.price).scaleb(-self.currency_minor_unit)

    @property
    def regular_amount(self) -> Decimal:
        if not self.regular_price:
            return self.current_amount
        return Decimal(self.regular_price).scaleb(-self.currency_minor_unit)


class WooCommerceImage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    src: Annotated[str, Field(max_length=2_048)] = ""
    thumbnail: Annotated[str, Field(max_length=2_048)] = ""


class WooCommerceCategory(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(gt=0)]
    name: Annotated[str, Field(min_length=1, max_length=256)]
    slug: Annotated[str, Field(max_length=256)] = ""

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        return _require_normalized_text(name)


class WooCommerceProduct(BaseModel):
    """Validated subset of one WooCommerce Store API product."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(gt=0)]
    name: Annotated[str, Field(min_length=1, max_length=512)]
    permalink: Annotated[str, Field(min_length=1, max_length=2_048)]
    sku: Annotated[str, Field(max_length=128)] = ""
    prices: WooCommercePrices
    images: Annotated[
        tuple[WooCommerceImage, ...],
        Field(max_length=MAX_WOOCOMMERCE_IMAGES_PER_PRODUCT),
    ] = ()
    categories: Annotated[
        tuple[WooCommerceCategory, ...],
        Field(max_length=MAX_WOOCOMMERCE_CATEGORIES_PER_PRODUCT),
    ] = ()
    is_in_stock: bool = False
    type: Annotated[str, Field(max_length=32)] = "simple"

    @field_validator("images", mode="before")
    @classmethod
    def require_bounded_images(cls, images: JsonValue) -> JsonValue:
        if (
            isinstance(images, list)
            and len(images) > MAX_WOOCOMMERCE_IMAGES_PER_PRODUCT
        ):
            msg = "too many product images"
            raise ValueError(msg)
        return images

    @field_validator("categories", mode="before")
    @classmethod
    def require_bounded_categories(cls, categories: JsonValue) -> JsonValue:
        if (
            isinstance(categories, list)
            and len(categories) > MAX_WOOCOMMERCE_CATEGORIES_PER_PRODUCT
        ):
            msg = "too many product categories"
            raise ValueError(msg)
        return categories

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        return _require_normalized_text(name)


@dataclass(frozen=True, slots=True)
class WooCommerceDiscoveryProduct:
    product: WooCommerceProduct
    published_at: datetime
    hostname: str


def first_party_product_url(url: str, hostname: str) -> str | None:
    """Return a product URL only when it stays on the configured shop."""
    if not _is_first_party_url(url, hostname, "/product/"):
        return None
    return url


def first_party_image_url(
    product: WooCommerceProduct,
    hostname: str,
) -> str | None:
    """Return the first first-party uploaded image, preferring thumbnails."""
    for image in product.images:
        for candidate in (image.thumbnail, image.src):
            if candidate and _is_first_party_url(
                candidate,
                hostname,
                "/wp-content/uploads/",
            ):
                return candidate
    return None


def _normalize_text(value: str) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in unescape(value)
    )
    return " ".join(printable.split())


def _require_normalized_text(value: str) -> str:
    normalized = _normalize_text(value)
    if not normalized:
        msg = "display text must not be empty"
        raise ValueError(msg)
    return normalized


def _is_first_party_url(url: str, hostname: str, path_prefix: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = 443 if parsed.port is None else parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == hostname
        and port == 443
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith(path_prefix)
        and not parsed.fragment
    )
