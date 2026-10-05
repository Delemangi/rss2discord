"""Validated Anhoch catalog product models."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
)

from rss2discord.anhoch_money import (
    MAX_ANHOCH_MONEY_DECIMAL_PLACES,
    MAX_ANHOCH_MONEY_DIGITS,
    canonicalize_anhoch_amount,
)

MAX_SQLITE_SIGNED_INTEGER: Final = 2**63 - 1


class AnhochMoney(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    amount: Annotated[
        Decimal,
        Field(
            ge=Decimal(0),
            allow_inf_nan=False,
            max_digits=MAX_ANHOCH_MONEY_DIGITS,
            decimal_places=MAX_ANHOCH_MONEY_DECIMAL_PLACES,
        ),
    ]
    currency: Annotated[str, Field(min_length=1)]
    formatted: Annotated[str, Field(min_length=1)]

    @field_validator("amount")
    @classmethod
    def require_persistable_amount(cls, amount: Decimal) -> Decimal:
        canonicalize_anhoch_amount(amount)
        return amount

    @property
    def persistable_amount(self) -> str:
        return canonicalize_anhoch_amount(self.amount)


class AnhochDisplayPrice(BaseModel):
    """Display-only catalog price without a required exact monetary value."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    formatted: Annotated[str, Field(min_length=1)]


class AnhochImage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    path: Annotated[str, Field(min_length=1)]


class AnhochInstallments(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    period: Annotated[int, Field(gt=0)]
    price: AnhochDisplayPrice


class AnhochProduct(BaseModel):
    """Validated subset of one product from the public catalog API."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(ge=1, le=MAX_SQLITE_SIGNED_INTEGER)]
    name: Annotated[str, Field(min_length=1)]
    slug: Annotated[str, Field(min_length=1)]
    price: AnhochDisplayPrice
    selling_price: AnhochMoney
    base_image: AnhochImage | None = None
    is_in_stock: bool
    qty: int | None = None
    installments: AnhochInstallments | None = None

    @field_validator("base_image", mode="before")
    @classmethod
    def normalize_empty_image(cls, value: JsonValue) -> JsonValue:
        if value == []:
            return None
        return value


class AnhochProductPage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current_page: Annotated[int, Field(gt=0)]
    last_page: Annotated[int, Field(gt=0)]
    data: tuple[AnhochProduct, ...]


class AnhochCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    products: AnhochProductPage
