"""Validated Neptun category and product API models."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rss2discord.price_amount import canonicalize_price_amount


class NeptunInitialSearchModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        alias_generator=lambda name: "".join(part.title() for part in name.split("_")),
        extra="ignore",
        frozen=True,
    )

    category_id: Annotated[int, Field(gt=0)]
    sort: int
    recomended: bool
    show_all_products: bool
    items_per_page: Annotated[int, Field(gt=0)]
    current_page: Annotated[int, Field(gt=0)]


class NeptunNamedValue(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    name: Annotated[str, Field(alias="Name", min_length=1)]


class NeptunCategory(NeptunNamedValue):
    url: Annotated[str, Field(alias="Url", min_length=1)]


class NeptunProduct(BaseModel):
    """Validated product fields consumed by discovery and price monitoring."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(alias="Id", gt=0)]
    title: Annotated[str, Field(alias="Title", min_length=1)]
    available_online: bool = Field(alias="AvailableOnline")
    available_webshop: bool = Field(alias="AvailableWebshop")
    active: bool = Field(alias="Active")
    manufacturer: NeptunNamedValue = Field(alias="Manufacturer")
    category: NeptunCategory = Field(alias="Category")
    code_number: Annotated[str, Field(alias="CodeNumber", min_length=1)]
    has_discount: bool = Field(alias="HasDiscount")
    regular_price: Annotated[Decimal, Field(alias="RegularPrice", ge=0)]
    discount_price: Annotated[Decimal, Field(alias="DiscountPrice", ge=0)]
    webshop_discount_price: Annotated[
        Decimal,
        Field(alias="WebshopDiscountPrice", ge=0),
    ]
    actual_price: Annotated[Decimal, Field(alias="ActualPrice", ge=0)]
    discount_percent: Annotated[Decimal, Field(alias="DiscountPercent", ge=0)]
    currency_label: Literal["ден."] = Field(alias="Currency")
    thumbnail: str = Field(alias="Thumbnail")
    url: Annotated[str, Field(alias="Url", min_length=1)]
    warranty: Annotated[int, Field(alias="Warranty", ge=0)]
    quantity: Annotated[int, Field(alias="Quantity", ge=0)]
    preorder: bool = Field(alias="Preorder")
    date_inserted: datetime = Field(alias="DateInserted")

    @field_validator(
        "regular_price",
        "discount_price",
        "webshop_discount_price",
        "actual_price",
    )
    @classmethod
    def require_persistable_price(cls, amount: Decimal) -> Decimal:
        canonicalize_price_amount(amount)
        return amount

    @field_validator("date_inserted")
    @classmethod
    def require_timezone_free_date_inserted(cls, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is not None:
            msg = "DateInserted must be timezone-free"
            raise ValueError(msg)
        return timestamp


class NeptunBatchConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    total_items: Annotated[int, Field(alias="TotalItems", ge=0)]


class NeptunBatch(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    config: NeptunBatchConfig = Field(alias="Config")
    items: tuple[NeptunProduct, ...] = Field(alias="Items")


class NeptunProductsResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    batch: NeptunBatch = Field(alias="Batch")
