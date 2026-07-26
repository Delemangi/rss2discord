"""Validated Setec catalog API models."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """Validated subset of one product from the Setec catalog API."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    handle: Annotated[str, Field(min_length=1)]
    thumbnail: str | None = None
    created_at: datetime
    variants: tuple[_SetecVariant, ...]
    categories: tuple[_SetecCategory, ...] = ()


class SetecCatalogResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    count: Annotated[int, Field(ge=0)]
    products: tuple[SetecProduct, ...]
