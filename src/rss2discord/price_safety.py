"""Bounded price-monitor constants."""

from typing import Final

MAX_INLINE_PRICE_CHANGES: Final = 25
HEALTH_REMINDER_SECONDS: Final = 21_600

__all__ = [
    "HEALTH_REMINDER_SECONDS",
    "MAX_INLINE_PRICE_CHANGES",
]
