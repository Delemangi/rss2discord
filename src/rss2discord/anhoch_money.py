"""Bounded exact-money rules shared by Anhoch parsing and persistence."""

from decimal import Decimal
from typing import Final

# Live MKD catalog amounts use at most four fractional digits; this permits
# twelve whole digits while bounding fixed-point persistence to 17 characters.
MAX_ANHOCH_MONEY_DIGITS: Final = 16
MAX_ANHOCH_MONEY_DECIMAL_PLACES: Final = 4
MAX_ANHOCH_MONEY_WHOLE_DIGITS: Final = (
    MAX_ANHOCH_MONEY_DIGITS - MAX_ANHOCH_MONEY_DECIMAL_PLACES
)
# Keep enough raw scale for harmless trailing zeros while preventing Decimal
# normalization from processing a hostile scientific exponent.
MIN_ANHOCH_MONEY_EXPONENT: Final = -MAX_ANHOCH_MONEY_DIGITS


def canonicalize_anhoch_amount(amount: Decimal) -> str:
    """Return safe canonical fixed-point text for a validated Anhoch amount."""
    if not amount.is_finite() or amount < 0:
        msg = "Anhoch money must be finite and non-negative"
        raise ValueError(msg)
    if amount.is_zero():
        return "0"
    _, digits, raw_exponent = amount.as_tuple()
    exponent = int(raw_exponent)
    significant_digits_end = len(digits)
    while digits[significant_digits_end - 1] == 0:
        significant_digits_end -= 1
        exponent += 1
    significant_digits = digits[:significant_digits_end]
    if exponent < MIN_ANHOCH_MONEY_EXPONENT:
        msg = "Anhoch money exceeds the supported precision"
        raise ValueError(msg)

    decimal_places = max(-int(exponent), 0)
    whole_digits = len(significant_digits) + exponent
    if (
        len(significant_digits) > MAX_ANHOCH_MONEY_DIGITS
        or decimal_places > MAX_ANHOCH_MONEY_DECIMAL_PLACES
        or whole_digits > MAX_ANHOCH_MONEY_WHOLE_DIGITS
    ):
        msg = "Anhoch money exceeds the supported precision"
        raise ValueError(msg)
    return format(Decimal((0, significant_digits, exponent)), "f")
