"""Bounded exact-price rules for persisted source-neutral snapshots."""

from decimal import Decimal
from typing import Final

MAX_PRICE_AMOUNT_DIGITS: Final = 24
MAX_PRICE_AMOUNT_DECIMAL_PLACES: Final = 12
MAX_PRICE_AMOUNT_WHOLE_DIGITS: Final = 12
MIN_PRICE_AMOUNT_EXPONENT: Final = -MAX_PRICE_AMOUNT_DIGITS


class PriceAmountValidationError(ValueError):
    """Raised when a snapshot amount cannot be safely canonicalized."""


def canonicalize_price_amount(amount: Decimal) -> str:
    """Return safe canonical fixed-point text for one persisted snapshot amount."""
    if not amount.is_finite() or amount < 0:
        raise PriceAmountValidationError("Price amount must be finite and non-negative")
    if amount.is_zero():
        return "0"

    _, digits, raw_exponent = amount.as_tuple()
    exponent = int(raw_exponent)
    significant_digits_end = len(digits)
    while digits[significant_digits_end - 1] == 0:
        significant_digits_end -= 1
        exponent += 1
    significant_digits = digits[:significant_digits_end]
    if exponent < MIN_PRICE_AMOUNT_EXPONENT:
        raise PriceAmountValidationError("Price amount exceeds the supported precision")

    decimal_places = max(-exponent, 0)
    whole_digits = len(significant_digits) + exponent
    if (
        len(significant_digits) > MAX_PRICE_AMOUNT_DIGITS
        or decimal_places > MAX_PRICE_AMOUNT_DECIMAL_PLACES
        or whole_digits > MAX_PRICE_AMOUNT_WHOLE_DIGITS
    ):
        raise PriceAmountValidationError("Price amount exceeds the supported precision")
    return format(Decimal((0, significant_digits, exponent)), "f")
