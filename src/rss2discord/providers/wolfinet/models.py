"""Normalized models extracted from a Wolfinet shop page."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class WolfinetProduct:
    """One immutable product discovered on a Wolfinet shop page."""

    product_id: str
    name: str
    url: str
    image_url: str | None
    description: str
    amount: Decimal
    formatted_price: str
    currency: str
    observed_at: datetime
