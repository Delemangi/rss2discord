"""Normalized models extracted from Ananas marketplace inventory hits."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

AnanasCurrency = Literal["MKD"]


@dataclass(frozen=True, slots=True)
class AnanasProduct:
    """One product after choosing the best marketplace offer for its slug."""

    inventory_id: str
    product_id: str
    name: str
    url: str
    image_url: str | None
    brand: str | None
    categories: tuple[str, ...]
    price: Decimal
    base_price: Decimal | None
    currency: AnanasCurrency
    in_stock: bool
    available: int | None
    merchant: str
    first_published_ms: int | None
    has_variations: bool
