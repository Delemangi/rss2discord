"""Comparison stage for source-neutral price monitoring."""

from collections.abc import Iterable, Mapping

from rss2discord.models import PriceDirection
from rss2discord.recovery_models import PriceChangeRecord, PriceSnapshot


def price_direction(
    previous: PriceSnapshot,
    current: PriceSnapshot,
) -> PriceDirection | None:
    """Report which way a monitored price moved.

    Returns ``None`` when the two amounts are not comparable, such as a change
    that crossed currencies, so callers never claim a direction the snapshots
    cannot establish.
    """
    if previous.currency != current.currency:
        return None
    if current.amount < previous.amount:
        return PriceDirection.DECREASE
    return PriceDirection.INCREASE


def diff_price_snapshots(
    current: Iterable[PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
) -> tuple[tuple[PriceSnapshot, ...], tuple[PriceChangeRecord, ...]]:
    """Return silent updates and price transitions in current observation order.

    New IDs and formatting-only changes are silent; amount or currency changes
    produce records even when their display text is unchanged. Equal snapshots
    and persisted IDs absent from current produce no work. This pure comparison
    does not filter held IDs, gate currencies, or persist anything: delivery
    planning owns those decisions.
    """
    silent: list[PriceSnapshot] = []
    changes: list[PriceChangeRecord] = []
    for snapshot in current:
        previous = persisted.get(snapshot.product_id)
        if previous is None:
            silent.append(snapshot)
        elif (
            previous.amount != snapshot.amount or previous.currency != snapshot.currency
        ):
            changes.append(PriceChangeRecord(snapshot.product_id, previous, snapshot))
        elif previous.formatted != snapshot.formatted:
            silent.append(snapshot)
    return tuple(silent), tuple(changes)
