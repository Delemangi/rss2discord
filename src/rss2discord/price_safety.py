"""Pure price-manifest safety checks used by price job adapters."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.recovery_models import (
    PriceBatch,
    PriceBatchItem,
    PriceBatchSummary,
    PriceChangeRecord,
    PriceSnapshot,
)

MAX_UNAPPROVED_PRICE_CHANGES: Final = 100
MAX_PRICE_MANIFEST_ITEMS: Final = 150_000
MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN: Final = 10
HEALTH_REMINDER_SECONDS: Final = 21_600
MANIFEST_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class PriceSafetyDecision:
    """Result of comparing stable IDs and source snapshots before delivery."""

    allowed: bool
    reason: str | None = None
    missing_product_ids: tuple[str, ...] = ()
    changed_product_ids: tuple[str, ...] = ()
    inconsistent_product_ids: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        """Compatibility spelling for callers that phrase the gate positively."""
        return self.allowed


def price_change_records(
    previous: Mapping[str, PriceSnapshot],
    current: Mapping[str, PriceSnapshot],
) -> tuple[PriceChangeRecord, ...]:
    """Return sorted changes for stable IDs present in both snapshots."""
    records = [
        PriceChangeRecord(
            product_id=product_id,
            previous=previous[product_id],
            current=snapshot,
        )
        for product_id, snapshot in current.items()
        if product_id in previous
        and (
            previous[product_id].amount != snapshot.amount
            or previous[product_id].currency != snapshot.currency
            or previous[product_id].formatted != snapshot.formatted
        )
    ]
    return tuple(sorted(records, key=lambda record: record.product_id))


def canonical_manifest_fingerprint(
    *,
    feed_id: str,
    provider: str,
    items: Iterable[PriceChangeRecord],
) -> str:
    """Hash the sorted canonical manifest, including every displayed price field."""
    manifest = [
        {
            "id": item.product_id,
            "old": _snapshot_payload(item.previous),
            "target": _snapshot_payload(item.current),
        }
        for item in sorted(items, key=lambda item: item.product_id)
    ]
    payload = {
        "version": MANIFEST_VERSION,
        "feed": feed_id,
        "provider": provider,
        "items": manifest,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


manifest_fingerprint = canonical_manifest_fingerprint
compute_manifest_fingerprint = canonical_manifest_fingerprint
PriceChangeDecision = PriceSafetyDecision


def assess_price_safety(
    *,
    expected: Iterable[PriceBatchItem],
    current: Mapping[str, PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
) -> PriceSafetyDecision:
    """Fail closed when an approved target moved, disappeared, or lost its prior."""
    missing: list[str] = []
    changed: list[str] = []
    inconsistent: list[str] = []
    for item in expected:
        product_id = item.product_id
        source = current.get(product_id)
        previous = persisted.get(product_id)
        if source is None:
            missing.append(product_id)
            continue
        if previous is None or not _same_snapshot(previous, item.previous):
            inconsistent.append(product_id)
        if not _same_snapshot(source, item.current):
            changed.append(product_id)
    if missing:
        return PriceSafetyDecision(False, "SourceMissing", tuple(sorted(missing)))
    if changed:
        return PriceSafetyDecision(
            False,
            "SourceMoved",
            changed_product_ids=tuple(sorted(changed)),
        )
    if inconsistent:
        return PriceSafetyDecision(
            False,
            "PreviousSnapshotInconsistent",
            inconsistent_product_ids=tuple(sorted(inconsistent)),
        )
    return PriceSafetyDecision(True)


validate_approved_price_batch = assess_price_safety


def _snapshot_payload(snapshot: PriceSnapshot | None) -> dict[str, str] | None:
    if snapshot is None:
        return None
    return {
        "amount": canonicalize_price_amount(snapshot.amount),
        "currency": snapshot.currency,
        "formatted": snapshot.formatted,
    }


def _same_snapshot(left: PriceSnapshot, right: PriceSnapshot) -> bool:
    return (
        left.product_id == right.product_id
        and canonicalize_price_amount(Decimal(left.amount))
        == canonicalize_price_amount(Decimal(right.amount))
        and left.currency == right.currency
        and left.formatted == right.formatted
    )


__all__ = [
    "HEALTH_REMINDER_SECONDS",
    "MANIFEST_VERSION",
    "MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN",
    "MAX_PRICE_MANIFEST_ITEMS",
    "MAX_UNAPPROVED_PRICE_CHANGES",
    "PriceBatch",
    "PriceBatchItem",
    "PriceBatchSummary",
    "PriceChangeDecision",
    "PriceChangeRecord",
    "PriceSafetyDecision",
    "PriceSnapshot",
    "assess_price_safety",
    "canonical_manifest_fingerprint",
    "compute_manifest_fingerprint",
    "manifest_fingerprint",
    "price_change_records",
    "validate_approved_price_batch",
]
