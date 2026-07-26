"""Validated Setec catalog API models."""

from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class _SetecCalculatedPrice(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    calculated_amount: Annotated[int, Field(ge=0)]
    original_amount: Annotated[int, Field(ge=0)]
    currency_code: Literal["mkd"]


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
