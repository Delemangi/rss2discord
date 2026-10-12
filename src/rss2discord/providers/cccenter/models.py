"""Normalized models extracted from CCCenter's WooCommerce HTML."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

CCCenterPriceStatus = Literal["scalar", "variable", "range", "unpriced"]


@dataclass(frozen=True, slots=True)
class CCCenterListing:
    product_id: str
    name: str
    url: str
    current_price: Decimal | None
    original_price: Decimal | None
    image_url: str | None
    price_status: CCCenterPriceStatus = "scalar"


@dataclass(frozen=True, slots=True)
class CCCenterProduct:
    product_id: str
    name: str
    url: str
    sku: str
    current_price: Decimal | None
    original_price: Decimal | None
    image_url: str | None
    categories: tuple[str, ...]
    is_in_stock: bool
    published_at: None = None
    price_status: CCCenterPriceStatus = "scalar"
