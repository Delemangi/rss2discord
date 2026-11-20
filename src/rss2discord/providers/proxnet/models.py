"""Normalized models extracted from a PROXNet category page."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ProxnetProduct:
    """One immutable product discovered on a PROXNet category page."""

    product_id: str
    name: str
    url: str
    image_url: str | None
    description: str
    amount: Decimal
    formatted_price: str
    currency: str
    observed_at: datetime
    category: str = ""


@dataclass(frozen=True, slots=True)
class ProxnetPage:
    """Products from one category page plus its display title."""

    products: tuple[ProxnetProduct, ...]
    title: str
