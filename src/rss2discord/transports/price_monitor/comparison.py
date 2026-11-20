"""Comparison stage for source-neutral price monitoring."""

from collections.abc import Iterable, Mapping

from rss2discord.models import PriceChangeRecord, PriceDirection, PriceSnapshot


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


def price_change_percentage(
    previous: PriceSnapshot,
    current: PriceSnapshot,
) -> str | None:
    """Return a signed percentage for comparable non-zero prices."""
    if previous.currency != current.currency or previous.amount <= 0:
        return None
    percent = (current.amount - previous.amount) / previous.amount * 100
    return f"{percent:+.1f}%"


def diff_price_snapshots(
    current: Iterable[PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
) -> tuple[tuple[PriceSnapshot, ...], tuple[PriceChangeRecord, ...]]:
    """Return silent updates and price transitions in current observation order.

    New IDs and formatting-only changes are silent; amount or currency changes
    produce records even when their display text is unchanged. A change in
    reported availability alone is also silent here so the caller can persist
    stock state without treating it as a price move. Equal snapshots and
    persisted IDs absent from current produce no work. This pure comparison does
    not filter held IDs, gate currencies, or persist anything: delivery planning
    owns those decisions.
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
        elif (
            previous.formatted != snapshot.formatted
            or previous.available != snapshot.available
        ):
            silent.append(snapshot)
    return tuple(silent), tuple(changes)


def diff_stock_snapshots(
    current: Iterable[PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
) -> tuple[tuple[PriceSnapshot, ...], tuple[PriceChangeRecord, ...]]:
    """Return silent stock updates and restock transitions in observation order.

    Only a persisted ``False`` turning into ``True`` is a restock. A new ID is
    left to the price diff as a silent baseline, an unknown persisted state is
    persisted silently because there is no prior stock claim to contradict, an
    out-of-stock observation is silent, and an observation that reports no
    availability establishes nothing. Decimal amount and currency are carried
    through unchanged so a restock can also report its price move.
    """
    silent: list[PriceSnapshot] = []
    restocks: list[PriceChangeRecord] = []
    for snapshot in current:
        previous = persisted.get(snapshot.product_id)
        if previous is None or snapshot.available is None:
            continue
        if previous.available is None:
            silent.append(snapshot)
        elif previous.available is snapshot.available:
            continue
        elif snapshot.available:
            restocks.append(PriceChangeRecord(snapshot.product_id, previous, snapshot))
        else:
            silent.append(snapshot)
    return tuple(silent), tuple(restocks)
