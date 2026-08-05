"""Validated WooCommerce Store API models for Hivetec."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from typing import Annotated, ClassVar, Final, Literal, Self
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

MAX_HIVETEC_CATEGORIES_PER_PRODUCT: Final = 16
MAX_HIVETEC_IMAGES_PER_PRODUCT: Final = 16


class HivetecPrices(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    price: Annotated[str, Field(pattern=r"^\d+$", max_length=24)]
    regular_price: Annotated[str, Field(pattern=r"^\d+$", max_length=24)]
    currency_code: Literal["MKD"]
    currency_minor_unit: Literal[2]

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
        return Decimal(self.regular_price).scaleb(-self.currency_minor_unit)


class HivetecImage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    src: Annotated[str, Field(min_length=1, max_length=2_048)]
    thumbnail: Annotated[str, Field(min_length=1, max_length=2_048)]


class HivetecCategory(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=256)]

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        return _require_normalized_text(name)


class HivetecProduct(BaseModel):
    """Validated subset of one Hivetec Store API product."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(gt=0)]
    name: Annotated[str, Field(min_length=1, max_length=512)]
    permalink: Annotated[str, Field(min_length=1, max_length=2_048)]
    sku: Annotated[str, Field(max_length=128)] = ""
    prices: HivetecPrices
    images: Annotated[
        tuple[HivetecImage, ...],
        Field(max_length=MAX_HIVETEC_IMAGES_PER_PRODUCT),
    ] = ()
    categories: Annotated[
        tuple[HivetecCategory, ...],
        Field(max_length=MAX_HIVETEC_CATEGORIES_PER_PRODUCT),
    ] = ()
    is_in_stock: bool

    @field_validator("images", mode="before")
    @classmethod
    def require_bounded_images(cls, images: JsonValue) -> JsonValue:
        if isinstance(images, list) and len(images) > MAX_HIVETEC_IMAGES_PER_PRODUCT:
            msg = "too many product images"
            raise ValueError(msg)
        return images

    @field_validator("categories", mode="before")
    @classmethod
    def require_bounded_categories(cls, categories: JsonValue) -> JsonValue:
        if (
            isinstance(categories, list)
            and len(categories) > MAX_HIVETEC_CATEGORIES_PER_PRODUCT
        ):
            msg = "too many product categories"
            raise ValueError(msg)
        return categories

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        return _require_normalized_text(name)

    @model_validator(mode="after")
    def require_first_party_product_url(self) -> Self:
        if not _is_first_party_url(self.permalink, "/product/"):
            msg = "product URL must be first-party"
            raise ValueError(msg)
        return self

    @property
    def image_url(self) -> str | None:
        """Return the first safe Hivetec thumbnail, falling back to its source image."""
        for image in self.images:
            if _is_first_party_url(image.thumbnail, "/wp-content/uploads/"):
                return image.thumbnail
            if _is_first_party_url(image.src, "/wp-content/uploads/"):
                return image.src
        return None


class HivetecProductDate(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(gt=0)]
    date_gmt: datetime
    status: Literal["publish"]

    @field_validator("date_gmt")
    @classmethod
    def normalize_date_to_utc(cls, date_gmt: datetime) -> datetime:
        if date_gmt.tzinfo is None:
            return date_gmt.replace(tzinfo=UTC)
        return date_gmt.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class HivetecDiscoveryProduct:
    product: HivetecProduct
    published_at: datetime


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


def _is_first_party_url(url: str, path_prefix: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = 443 if parsed.port is None else parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "hivetec.mk"
        and port == 443
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith(path_prefix)
        and not parsed.query
        and not parsed.fragment
    )
