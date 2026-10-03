from decimal import Decimal

from rss2discord.price_safety import (
    MAX_UNAPPROVED_PRICE_CHANGES,
    assess_price_safety,
    canonical_manifest_fingerprint,
    price_change_records,
)
from rss2discord.recovery_models import PriceBatchItem, PriceSnapshot


def snapshot(
    product_id: str,
    amount: str,
    formatted: str | None = None,
) -> PriceSnapshot:
    return PriceSnapshot(
        "feed",
        product_id,
        Decimal(amount),
        formatted or f"{amount} EUR",
        "EUR",
    )


def test_manifest_is_sorted_and_includes_formatted_target() -> None:
    first = snapshot("a", "1")
    second = snapshot("a", "2", "two EUR")
    record = price_change_records({"a": first}, {"a": second})
    reversed_record = tuple(reversed(record))

    assert record == reversed_record
    assert canonical_manifest_fingerprint(
        feed_id="feed",
        provider="provider",
        items=record,
    ) != canonical_manifest_fingerprint(
        feed_id="feed",
        provider="provider",
        items=(type(record[0])("a", first, snapshot("a", "2", "other EUR")),),
    )


def test_approved_safety_fails_closed_on_missing_moved_or_prior_mismatch() -> None:
    old = snapshot("a", "1")
    target = snapshot("a", "2")
    item = PriceBatchItem("a", old, target)

    assert (
        assess_price_safety(expected=(item,), current={}, persisted={}).reason
        == "SourceMissing"
    )
    assert (
        assess_price_safety(
            expected=(item,),
            current={"a": snapshot("a", "3")},
            persisted={"a": old},
        ).reason
        == "SourceMoved"
    )
    assert (
        assess_price_safety(
            expected=(item,),
            current={"a": target},
            persisted={"a": snapshot("a", "9")},
        ).reason
        == "PreviousSnapshotInconsistent"
    )


def test_unapproved_limit_is_explicit() -> None:
    assert MAX_UNAPPROVED_PRICE_CHANGES == 100
