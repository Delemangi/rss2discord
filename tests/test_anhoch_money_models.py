from decimal import Decimal

import pytest
from pydantic import ValidationError

import rss2discord.transports.anhoch as anhoch_transport
from rss2discord.anhoch_money import canonicalize_anhoch_amount
from rss2discord.transports.anhoch_models import AnhochMoney
from tests.anhoch_helpers import product_payload


def test_anhoch_product_money_parses_decimal_amount_and_canonicalizes() -> None:
    # Given
    payload = product_payload(
        1,
        "product-0001",
        selling_price_amount="1.20",
        selling_price_currency="MKD",
    )

    # When
    product = anhoch_transport.AnhochProduct.model_validate(payload)
    same_product = anhoch_transport.AnhochProduct.model_validate(
        product_payload(
            2,
            "product-0002",
            selling_price_amount="1.2",
            selling_price_currency="MKD",
        ),
    )

    # Then
    assert product.selling_price.amount == Decimal("1.20")
    assert product.selling_price.currency == "MKD"
    assert product.selling_price.formatted == "1.200,00 ден."
    assert product.selling_price.persistable_amount == "1.2"
    assert (
        product.selling_price.persistable_amount
        == same_product.selling_price.persistable_amount
    )


def test_accepted_high_scale_zero_persists_as_canonical_zero() -> None:
    # Given
    money = AnhochMoney(
        amount=Decimal("0E-1000000"),
        currency="MKD",
        formatted="0 ден.",
    )

    # When
    persisted_amount = money.persistable_amount

    # Then
    assert persisted_amount == "0"


def test_accepted_high_scale_nonzero_persists_as_canonical_amount() -> None:
    # Given
    money = AnhochMoney(
        amount=Decimal("1.00000000000000000"),
        currency="MKD",
        formatted="1 ден.",
    )

    # When
    persisted_amount = money.persistable_amount

    # Then
    assert persisted_amount == "1"


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("-1.20"),
        "not-a-number",
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_anhoch_product_money_rejects_invalid_amounts(amount: str | Decimal) -> None:
    # Given
    payload = product_payload(1, "product-0001")
    payload["selling_price"] = {
        "amount": amount,
        "currency": "MKD",
        "formatted": "1.200,00 ден.",
    }

    # When / Then
    with pytest.raises(ValidationError) as validation_error:
        anhoch_transport.AnhochProduct.model_validate(payload)

    assert validation_error.value.errors()[0]["loc"] == ("selling_price", "amount")


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("1E+1000000"),
        Decimal("1E-1000000"),
        Decimal("999999999999.99999"),
    ],
)
def test_anhoch_product_rejects_unsafe_exact_money_before_canonicalization(
    amount: Decimal,
) -> None:
    # Given
    payload = product_payload(1, "product-0001", selling_price_amount=amount)

    # When / Then
    with pytest.raises(ValidationError):
        anhoch_transport.AnhochProduct.model_validate(payload)


def test_anhoch_product_accepts_four_decimal_mkd_selling_price() -> None:
    # Given
    amount = Decimal("999999999999.9999")
    payload = product_payload(1, "product-0001", selling_price_amount=amount)

    # When
    product = anhoch_transport.AnhochProduct.model_validate(payload)

    # Then
    assert product.selling_price.amount == amount


@pytest.mark.parametrize(
    "selling_price",
    [
        {"formatted": "1.200,00 ден."},
        {"amount": "1200.00", "formatted": "1.200,00 ден."},
        {"currency": "MKD", "formatted": "1.200,00 ден."},
    ],
)
def test_anhoch_product_requires_exact_selling_price(
    selling_price: dict[str, str],
) -> None:
    # Given
    payload = product_payload(1, "product-0001")
    payload["selling_price"] = selling_price

    # When / Then
    with pytest.raises(ValidationError):
        anhoch_transport.AnhochProduct.model_validate(payload)


def test_anhoch_money_rejects_extreme_negative_exponents_before_normalizing() -> None:
    # Given
    amount = Decimal("1E-1000000")

    # When / Then
    with pytest.raises(ValueError, match="supported precision"):
        canonicalize_anhoch_amount(amount)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(Decimal("1.2E+3"), "1200"), (Decimal("1E-4"), "0.0001")],
)
def test_anhoch_money_preserves_supported_scientific_notation(
    amount: Decimal,
    expected: str,
) -> None:
    # Given / When
    result = canonicalize_anhoch_amount(amount)

    # Then
    assert result == expected
