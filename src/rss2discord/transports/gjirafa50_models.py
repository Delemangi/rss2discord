"""Typed Gjirafa50 catalog values."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Gjirafa50Product:
    id: int
    title: str
    link: str
    image_url: str | None
    price: Decimal
    formatted_price: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Gjirafa50PriceRange:
    minimum_cents: int
    maximum_exclusive_cents: int

    def __str__(self) -> str:
        return (
            f"{_format_price_cents(self.minimum_cents)}-"
            f"{_format_price_cents(self.maximum_exclusive_cents - 1)}"
        )


def _format_price_cents(cents: int) -> str:
    whole, fraction = divmod(cents, 100)
    return str(whole) if fraction == 0 else f"{whole},{fraction:02d}"


@dataclass(frozen=True, slots=True)
class Gjirafa50CatalogPage:
    total_hits: int
    total_pages: int
    products: tuple[Gjirafa50Product, ...]
