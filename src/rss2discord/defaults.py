"""Shared application defaults."""

from typing import Final

DEFAULT_PRICE_INLINE_LIMIT: Final = 100
PRICE_HISTORY_WINDOW_DAYS: Final = 30
# Ordered from shortest to longest; ``None`` means the product's full history.
PRICE_HISTORY_WINDOWS: Final = (
    (30, "30-day"),
    (90, "90-day"),
    (180, "6-month"),
    (365, "1-year"),
    (None, "all-time"),
)
