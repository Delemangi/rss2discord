"""Bounded exact-money rules for persisted and provider snapshot amounts."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

MAX_PRICE_AMOUNT_DIGITS: Final = 24
MAX_PRICE_AMOUNT_DECIMAL_PLACES: Final = 12
MAX_PRICE_AMOUNT_WHOLE_DIGITS: Final = 12
MIN_PRICE_AMOUNT_EXPONENT: Final = -MAX_PRICE_AMOUNT_DIGITS

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


class PriceAmountValidationError(ValueError):
    """Raised when a snapshot amount cannot be safely canonicalized."""


@dataclass(frozen=True, slots=True)
class _CanonicalizationRules:
    """Digit and exponent bounds plus the failure contract for one money type."""

    max_digits: int
    max_decimal_places: int
    max_whole_digits: int
    min_exponent: int
    non_negative_message: str
    precision_message: str
    error_type: type[ValueError]


_PRICE_AMOUNT_RULES = _CanonicalizationRules(
    max_digits=MAX_PRICE_AMOUNT_DIGITS,
    max_decimal_places=MAX_PRICE_AMOUNT_DECIMAL_PLACES,
    max_whole_digits=MAX_PRICE_AMOUNT_WHOLE_DIGITS,
    min_exponent=MIN_PRICE_AMOUNT_EXPONENT,
    non_negative_message="Price amount must be finite and non-negative",
    precision_message="Price amount exceeds the supported precision",
    error_type=PriceAmountValidationError,
)

_ANHOCH_AMOUNT_RULES = _CanonicalizationRules(
    max_digits=MAX_ANHOCH_MONEY_DIGITS,
    max_decimal_places=MAX_ANHOCH_MONEY_DECIMAL_PLACES,
    max_whole_digits=MAX_ANHOCH_MONEY_WHOLE_DIGITS,
    min_exponent=MIN_ANHOCH_MONEY_EXPONENT,
    non_negative_message="Anhoch money must be finite and non-negative",
    precision_message="Anhoch money exceeds the supported precision",
    error_type=ValueError,
)


def canonicalize_price_amount(amount: Decimal) -> str:
    """Return safe canonical fixed-point text for one persisted snapshot amount."""
    return _canonicalize(amount, _PRICE_AMOUNT_RULES)


def canonicalize_anhoch_amount(amount: Decimal) -> str:
    """Return safe canonical fixed-point text for a validated Anhoch amount."""
    return _canonicalize(amount, _ANHOCH_AMOUNT_RULES)


def _canonicalize(amount: Decimal, rules: _CanonicalizationRules) -> str:
    if not amount.is_finite() or amount < 0:
        raise rules.error_type(rules.non_negative_message)
    if amount.is_zero():
        return "0"

    _, digits, raw_exponent = amount.as_tuple()
    exponent = int(raw_exponent)
    significant_digits_end = len(digits)
    while digits[significant_digits_end - 1] == 0:
        significant_digits_end -= 1
        exponent += 1
    significant_digits = digits[:significant_digits_end]
    if exponent < rules.min_exponent:
        raise rules.error_type(rules.precision_message)

    decimal_places = max(-exponent, 0)
    whole_digits = len(significant_digits) + exponent
    if (
        len(significant_digits) > rules.max_digits
        or decimal_places > rules.max_decimal_places
        or whole_digits > rules.max_whole_digits
    ):
        raise rules.error_type(rules.precision_message)
    return format(Decimal((0, significant_digits, exponent)), "f")
