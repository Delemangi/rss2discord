"""Per-feed filters that decide which detected price changes are worth alerting."""

from collections.abc import Sequence
from decimal import Decimal

from rss2discord.configuration import FeedConfig
from rss2discord.models import PriceChangeRecord


def _is_suppressed(record: PriceChangeRecord, feed: FeedConfig) -> bool:
    """Report whether one detected change falls below the feed's alert bar.

    A change is suppressed when any configured filter matches. Comparisons need
    a previous amount greater than zero in the same currency; otherwise the
    change alerts because its size cannot be established.
    """
    previous = record.previous
    current = record.current
    if previous.currency != current.currency or previous.amount <= 0:
        return False
    delta = current.amount - previous.amount
    if feed.price_ignore_increases and delta > 0:
        return True
    if feed.price_min_change_percentage > 0:
        threshold = Decimal(str(feed.price_min_change_percentage))
        if abs(delta) / previous.amount * 100 < threshold:
            return True
    minimum = feed.price_min_change_amount
    return minimum is not None and abs(delta) < minimum


def split_price_alert_changes(
    records: Sequence[PriceChangeRecord],
    feed: FeedConfig,
) -> tuple[tuple[PriceChangeRecord, ...], tuple[PriceChangeRecord, ...]]:
    """Split detected changes into alerting and silently baselined records.

    Suppressed records keep the new snapshot so callers can advance the stored
    baseline without notifying: a filtered change must not be re-evaluated on
    every later scan. New products and formatting-only changes never reach this
    filter because they are never price changes.
    """
    alerting: list[PriceChangeRecord] = []
    suppressed: list[PriceChangeRecord] = []
    for record in records:
        target = suppressed if _is_suppressed(record, feed) else alerting
        target.append(record)
    return tuple(alerting), tuple(suppressed)
