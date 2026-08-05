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
    minimum: int
    maximum: int

    def __str__(self) -> str:
        return f"{self.minimum}-{self.maximum}"


@dataclass(frozen=True, slots=True)
class Gjirafa50CatalogPage:
    total_hits: int
    total_pages: int
    products: tuple[Gjirafa50Product, ...]
