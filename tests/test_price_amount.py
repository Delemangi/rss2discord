from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import PriceSnapshot
from rss2discord.price_amount import PriceAmountValidationError, format_mkd


def test_price_snapshot_persists_source_neutral_precision_limit(tmp_path: Path) -> None:
    # Given
    amount = Decimal("999999999999.999999999999")
    snapshot = PriceSnapshot(
        feed_id="setec",
        product_id="prod-1",
        amount=amount,
        formatted="999.999.999.999,999999999999 ден.",
        currency="MKD",
    )

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot)
        persisted_snapshots = store.load_price_snapshots("setec")

    # Then
    assert persisted_snapshots == (snapshot,)


@pytest.mark.parametrize(
    "amount",
    [
        Decimal(-1),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1E-1000000"),
        Decimal("1E-13"),
        Decimal(1_000_000_000_000),
        Decimal("999999999999.9999999999999"),
    ],
)
def test_price_snapshot_rejects_amounts_outside_source_neutral_bounds(
    tmp_path: Path,
    amount: Decimal,
) -> None:
    # Given
    snapshot = PriceSnapshot(
        feed_id="setec",
        product_id="prod-1",
        amount=amount,
        formatted="invalid",
        currency="MKD",
    )

    # When / Then
    with (
        DeliveryStore(tmp_path / "state.db") as store,
        pytest.raises(PriceAmountValidationError),
    ):
        store.upsert_price_snapshot(snapshot)


def test_format_mkd_groups_whole_digits_and_trims_zeroes() -> None:
    assert format_mkd(Decimal("1234567.50")) == "1.234.567,5 \u0434\u0435\u043d."
    assert format_mkd(Decimal(1000)) == "1.000 \u0434\u0435\u043d."


def test_format_mkd_can_force_a_fraction() -> None:
    assert format_mkd(Decimal(1000), force_fraction=True) == (
        "1.000,00 \u0434\u0435\u043d."
    )
    assert format_mkd(Decimal("1000.5"), force_fraction=True) == (
        "1.000,5 \u0434\u0435\u043d."
    )
