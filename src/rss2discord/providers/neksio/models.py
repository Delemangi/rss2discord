"""Validated models for Neksio's public product-card response."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from rss2discord.anhoch_money import (
    MAX_ANHOCH_MONEY_DECIMAL_PLACES,
    MAX_ANHOCH_MONEY_DIGITS,
)

MAX_SQLITE_SIGNED_INTEGER: Final = 2**63 - 1
_CONTROL_CHARACTER_PATTERN: Final = re.compile(r"[\x00-\x1f\x7f]+")


def _normalize_display_text(value: str) -> str:
    return _CONTROL_CHARACTER_PATTERN.sub(" ", value).strip()


type NeksioDisplayText = Annotated[str, AfterValidator(_normalize_display_text)]


class NeksioProduct(BaseModel):
    """One observed Neksio catalog product with its current tax-inclusive price."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    product_id: Annotated[int, Field(ge=1, le=MAX_SQLITE_SIGNED_INTEGER)]
    product_name: Annotated[NeksioDisplayText, Field(min_length=1)]
    product_code: NeksioDisplayText
    category: NeksioDisplayText
    subcategory: NeksioDisplayText
    manufacturer: NeksioDisplayText
    price_with_tax: Annotated[
        Decimal,
        Field(
            ge=Decimal(0),
            max_digits=MAX_ANHOCH_MONEY_DIGITS,
            decimal_places=MAX_ANHOCH_MONEY_DECIMAL_PLACES,
            allow_inf_nan=False,
        ),
    ]
    formatted_price: Annotated[NeksioDisplayText, Field(min_length=1, max_length=128)]
    old_formatted_price: Annotated[NeksioDisplayText | None, Field(max_length=128)] = (
        None
    )
    image_path: Annotated[str, Field(min_length=1)]
    stock_quantity: Annotated[int, Field(ge=0)]
    observed_at: datetime


class NeksioProductCard(BaseModel):
    """The validated product fields returned by the Neksio card endpoint."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    product_id: Annotated[
        int,
        Field(validation_alias="productId", ge=1, le=MAX_SQLITE_SIGNED_INTEGER),
    ]
    product_name: Annotated[
        NeksioDisplayText,
        Field(validation_alias="productName", min_length=1),
    ]
    product_code: Annotated[NeksioDisplayText, Field(validation_alias="productCode")]
    category: NeksioDisplayText
    subcategory: Annotated[
        NeksioDisplayText | None,
        Field(validation_alias="subCategory"),
    ] = None
    manufacturer: NeksioDisplayText | None = None
    price_with_tax: Annotated[
        Decimal,
        Field(
            validation_alias="priceWTax",
            ge=Decimal(0),
            max_digits=MAX_ANHOCH_MONEY_DIGITS,
            decimal_places=MAX_ANHOCH_MONEY_DECIMAL_PLACES,
            allow_inf_nan=False,
        ),
    ]
    formatted_price: Annotated[
        NeksioDisplayText,
        Field(validation_alias="priceWTax_f", min_length=1, max_length=128),
    ]
    old_formatted_price: Annotated[
        NeksioDisplayText | None,
        Field(validation_alias="old_PriceWTax", max_length=128),
    ] = None
    image_path: Annotated[str, Field(validation_alias="imagePath", min_length=1)]
    stock_quantity: Annotated[int, Field(validation_alias="quantity", ge=-1)]

    def observe(self, observed_at: datetime) -> NeksioProduct:
        """Attach the single scan observation time to this validated API card."""
        return NeksioProduct(
            product_id=self.product_id,
            product_name=self.product_name,
            product_code=self.product_code,
            category=self.category,
            subcategory=self.subcategory or "",
            manufacturer=self.manufacturer or "",
            price_with_tax=self.price_with_tax,
            formatted_price=self.formatted_price,
            old_formatted_price=self.old_formatted_price,
            image_path=self.image_path,
            stock_quantity=0 if self.stock_quantity == -1 else self.stock_quantity,
            observed_at=observed_at,
        )


class NeksioCatalogPage(BaseModel):
    """Validated pagination metadata and product cards from one Neksio response."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    category_id: Annotated[int, Field(validation_alias="categoryId", ge=1)]
    page: Annotated[int, Field(ge=1)]
    page_size: Annotated[int, Field(validation_alias="pageSize", ge=1)]
    no_of_pages: Annotated[int, Field(validation_alias="noOfPages", ge=0)]
    no_of_products: Annotated[int, Field(validation_alias="noOfProducts", ge=0)]
    product_cards: tuple[NeksioProductCard, ...] = Field(
        validation_alias="productCards",
    )
