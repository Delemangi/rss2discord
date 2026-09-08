"""Normalized models extracted from Technomarket listing HTML."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class TechnomarketProduct:
    product_id: str
    name: str
    url: str
    image_url: str | None
    manufacturer: str | None
    categories: tuple[str, ...]
    regular_price: Decimal | None
    smart_price: Decimal | None

    @property
    def effective_price(self) -> Decimal | None:
        """Return the SMART price, falling back to the regular price."""
        return self.smart_price if self.smart_price is not None else self.regular_price
