from dataclasses import replace
from decimal import Decimal

import pytest

from rss2discord.delivery_store import PriceSnapshot
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.transports.price_monitor import diff_price_snapshots


@pytest.mark.parametrize(
    ("amount", "currency", "formatted", "kind"),
    [
        ("100.00", "MKD", "100 den", "equal"),
        ("100", "MKD", "100.00 ден.", "silent"),
        ("90", "MKD", "100 den", "change"),
        ("110", "MKD", "110 den", "change"),
        ("100", "EUR", "100 den", "change"),
    ],
)
def test_diff_classifies_amount_currency_and_display_independently(
    amount: str,
    currency: str,
    formatted: str,
    kind: str,
) -> None:
    previous = PriceSnapshot("feed", "1", Decimal(100), "100 den", "MKD")
    current = replace(
        previous,
        amount=Decimal(amount),
        currency=currency,
        formatted=formatted,
    )
    persisted = {"1": previous}

    silent, changes = diff_price_snapshots(iter((current,)), persisted)

    assert silent == ((current,) if kind == "silent" else ())
    assert changes == (
        (PriceChangeRecord("1", previous, current),) if kind == "change" else ()
    )
    assert persisted == {"1": previous}


def test_diff_preserves_observation_order_and_ignores_missing_history() -> None:
    baseline = PriceSnapshot("feed", "1", Decimal(100), "100 den", "MKD")
    persisted = {str(i): replace(baseline, product_id=str(i)) for i in (1, 2, 3)}
    changed_three = replace(persisted["3"], amount=Decimal(90))
    fresh = replace(baseline, product_id="4")
    changed_one = replace(baseline, amount=Decimal(110))
    reformatted = replace(persisted["2"], formatted="100.00 den")
    missing = replace(baseline, product_id="missing")
    persisted["missing"] = missing

    silent, changes = diff_price_snapshots(
        (changed_three, fresh, changed_one, reformatted),
        persisted,
    )

    assert silent == (fresh, reformatted)
    assert changes == (
        PriceChangeRecord("3", persisted["3"], changed_three),
        PriceChangeRecord("1", persisted["1"], changed_one),
    )
    assert persisted["missing"] == missing
    assert diff_price_snapshots((), persisted) == ((), ())
