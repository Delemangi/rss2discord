"""Validated DDStore GraphQL response models."""

from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from typing import Annotated, ClassVar, Final, Literal, Self, cast
from urllib.parse import urljoin, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from rss2discord.price_amount import canonicalize_price_amount

type DDStoreStockStatus = Literal["IN_STOCK", "OUT_OF_STOCK"]
DDSTORE_PRODUCT_BASE_URL = "https://ddstore.mk/"
MAX_DDSTORE_CATEGORIES_PER_PRODUCT: Final = 64


class _DDStoreMoney(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    value: Annotated[Decimal, Field(ge=0)]
    currency: Literal["MKD"]

    @field_validator("value")
    @classmethod
    def require_persistable_amount(cls, amount: Decimal) -> Decimal:
        return Decimal(canonicalize_price_amount(amount))


class _DDStoreMinimumPrice(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    final_price: _DDStoreMoney
    regular_price: _DDStoreMoney | None = None

    @model_validator(mode="after")
    def require_consistent_prices(self) -> Self:
        if (
            self.regular_price is not None
            and self.regular_price.value < self.final_price.value
        ):
            msg = "regular price cannot be lower than final price"
            raise ValueError(msg)
        return self


class _DDStorePriceRange(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    minimum_price: _DDStoreMinimumPrice


class _DDStoreImage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    url: Annotated[str, Field(min_length=1, max_length=2_048)] | None = None

    @field_validator("url")
    @classmethod
    def retain_first_party_url(cls, url: str | None) -> str | None:
        return url if url is None or _is_first_party_image_url(url) else None


class DDStoreCategory(BaseModel):
    """One validated DDStore category attached to a product."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str | None) -> str | None:
        normalized = _normalize_display_text(name) if name is not None else None
        return normalized or None


class DDStoreProduct(BaseModel):
    """Validated subset of one DDStore GraphQL catalog product."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    uid: Annotated[str, Field(min_length=1, max_length=256)]
    name: Annotated[str, Field(min_length=1, max_length=512)]
    url_key: Annotated[str, Field(min_length=1, max_length=2_048)]
    url_suffix: Annotated[str, Field(max_length=32)] | None = None
    created_at: datetime
    stock_status: DDStoreStockStatus
    small_image: _DDStoreImage | None = None
    categories: Annotated[
        tuple[DDStoreCategory, ...],
        Field(max_length=MAX_DDSTORE_CATEGORIES_PER_PRODUCT),
    ] | None = None
    price_range: _DDStorePriceRange

    @field_validator("categories", mode="before")
    @classmethod
    def require_bounded_categories(cls, categories: JsonValue) -> JsonValue:
        if (
            isinstance(categories, list)
            and len(categories) > MAX_DDSTORE_CATEGORIES_PER_PRODUCT
        ):
            msg = "too many product categories"
            raise ValueError(msg)
        return categories

    @field_validator("created_at")
    @classmethod
    def normalize_created_at_to_utc(cls, created_at: datetime) -> datetime:
        if created_at.tzinfo is None:
            return created_at.replace(tzinfo=UTC)
        return created_at.astimezone(UTC)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        return _normalize_display_text(name)

    @model_validator(mode="after")
    def require_first_party_product_url(self) -> Self:
        if (
            not _has_visible_text(self.uid)
            or not self.name
            or not _has_visible_text(self.url_key)
            or self.product_url is None
        ):
            msg = "product URL must be first-party"
            raise ValueError(msg)
        return self

    @property
    def product_url(self) -> str:
        """Return the validated first-party URL for this product."""
        return cast(str, _first_party_product_url(self.url_key, self.url_suffix))


class _DDStorePageInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    current_page: Annotated[int, Field(ge=1)]
    page_size: Annotated[int, Field(ge=1)]
    total_pages: Annotated[int, Field(ge=1)]


class _DDStoreProducts(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    total_count: Annotated[int, Field(ge=0)]
    items: tuple[DDStoreProduct, ...]
    page_info: _DDStorePageInfo


class _DDStoreData(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    products: _DDStoreProducts


class _GraphQLError(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    message: Annotated[str, Field(min_length=1)]


class DDStoreCatalogResponse(BaseModel):
    """Validated DDStore GraphQL response envelope."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    data: _DDStoreData
    errors: tuple[_GraphQLError, ...] = ()

    @model_validator(mode="after")
    def reject_graphql_errors(self) -> Self:
        if self.errors:
            msg = "GraphQL response contains errors"
            raise ValueError(msg)
        return self

    @property
    def products(self) -> _DDStoreProducts:
        """Return the validated catalog result."""
        return self.data.products


def _normalize_display_text(value: str) -> str:
    printable_text = "".join(
        character if character.isprintable() else " " for character in unescape(value)
    )
    return " ".join(printable_text.split())


def _first_party_product_url(url_key: str, url_suffix: str | None) -> str | None:
    try:
        parsed_key = urlsplit(url_key)
    except ValueError:
        return None
    decoded_segments = parsed_key.path.split("/")
    if (
        parsed_key.scheme
        or parsed_key.netloc
        or parsed_key.query
        or parsed_key.fragment
        or "\\" in url_key
        or not all(character.isprintable() for character in url_key)
        or any(segment in {".", ".."} for segment in decoded_segments)
        or (
            url_suffix is not None
            and (
                any(token in url_suffix for token in ("/", "\\", "?", "#"))
                or not all(character.isprintable() for character in url_suffix)
            )
        )
    ):
        return None
    resolved_url = urljoin(DDSTORE_PRODUCT_BASE_URL, f"{url_key}{url_suffix or ''}")
    return _safe_url(resolved_url, "ddstore.mk")


def _is_first_party_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port or 443
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and hostname == "ddstore.mk"
        and port == 443
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _safe_url(url: str, hostname: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != hostname
        or port != 443
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return url


def _has_visible_text(value: str) -> bool:
    return any(
        character.isprintable() and not character.isspace() for character in value
    )
