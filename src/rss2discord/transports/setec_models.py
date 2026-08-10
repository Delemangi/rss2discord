"""Validated Setec search API models."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
)

from rss2discord.price_amount import canonicalize_price_amount


class _SetecCalculatedPrice(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    calculated_amount: Annotated[Decimal, Field(ge=0)]
    original_amount: Annotated[Decimal, Field(ge=0)]
    currency_code: Literal["mkd"]

    @field_validator("calculated_amount", "original_amount")
    @classmethod
    def require_persistable_amount(cls, amount: Decimal) -> Decimal:
        canonicalize_price_amount(amount)
        return amount


class _SetecVariant(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    calculated_price: _SetecCalculatedPrice


class _SetecCategory(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    name: Annotated[str, Field(min_length=1)]


class SetecProduct(BaseModel):
    """Validated subset of one product from the Setec search index."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    handle: Annotated[str, Field(min_length=1)]
    thumbnail: str | None = None
    created_at: datetime
    variants: tuple[_SetecVariant, ...]
    categories: Annotated[
        tuple[_SetecCategory, ...],
        Field(validation_alias=AliasChoices("product_categories", "categories")),
    ] = ()


class _SetecIndexedPrice(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    calculated_amount: Annotated[Decimal, Field(ge=0)]
    currency_code: Literal["mkd"]

    @field_validator("calculated_amount")
    @classmethod
    def require_persistable_amount(cls, amount: Decimal) -> Decimal:
        canonicalize_price_amount(amount)
        return amount


class _SetecIndexedVariant(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    calculated_price: _SetecIndexedPrice


class SetecPriceEntry(BaseModel):
    """Validated identifier-and-price projection of one indexed Setec product."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[str, Field(min_length=1)]
    variants: tuple[_SetecIndexedVariant, ...] = ()

    @property
    def calculated_amount(self) -> Decimal | None:
        """Return the first variant's calculated amount, or None when unpriced."""
        if not self.variants:
            return None
        return self.variants[0].calculated_price.calculated_amount


class SetecFacetStats(BaseModel):
    """Numeric bounds a search reports for one faceted field."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    minimum: Annotated[
        float,
        Field(validation_alias=AliasChoices("min", "minimum"), allow_inf_nan=False),
    ]
    maximum: Annotated[
        float,
        Field(validation_alias=AliasChoices("max", "maximum"), allow_inf_nan=False),
    ]


class SetecCountResponse(BaseModel):
    """Facet-only search response used to count a band without fetching documents."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    facet_distribution: dict[str, dict[str, int]] = Field(
        validation_alias=AliasChoices("facetDistribution", "facet_distribution"),
    )
    facet_stats: dict[str, SetecFacetStats] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("facetStats", "facet_stats"),
    )

    def count_for(self, field: str) -> int | None:
        """Return the field's exact, uncapped count, or None when it is absent.

        An empty bucket means nothing matched, whereas a missing one means the
        response never carried the facet and its count cannot be trusted.
        """
        bucket = self.facet_distribution.get(field)
        if bucket is None:
            return None
        return sum(bucket.values())

    def bounds_for(self, field: str) -> SetecFacetStats | None:
        """Return the field's numeric bounds, or None when no document matched."""
        return self.facet_stats.get(field)


class SetecPriceIndexResponse(BaseModel):
    """Search response carrying the identifier-and-price projection."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    hits: tuple[SetecPriceEntry, ...]


class SetecProductResponse(BaseModel):
    """Search response carrying the full display projection."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    hits: tuple[SetecProduct, ...]


class SetecRawHitsResponse(BaseModel):
    """Search response whose hits are validated one at a time by the caller.

    Used for the product lookup, where one unusable document must be skipped like
    an absent one rather than discarding every other product in its batch.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    hits: tuple[JsonValue, ...]
