"""Per-scan discovery entry tracking: dedupe, watermark filtering, and touch."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from rss2discord.models import EntryId
from rss2discord.transports.base import ScraperStrategy

logger = logging.getLogger("rss2discord.app")


class DiscoveryCounters(Protocol):
    """Mutable counters shared with the discovery scan."""

    missing_id: int
    duplicates: int
    watermark_skipped: int
    unhandled: int


def _numeric_entry_id(entry_id: EntryId) -> int | None:
    try:
        return int(entry_id)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class DiscoveryEntryTracker:
    """Track one scan's seen, delivered, and watermark-filtered entries."""

    feed_id: str
    delivered: set[str]
    watermark: int | None = None
    uses_monotonic_entry_ids: bool = False
    seen: set[EntryId] = field(default_factory=set)
    delivered_seen: set[EntryId] = field(default_factory=set)

    def unhandled_entry_id(
        self,
        strategy: ScraperStrategy,
        entry: Any,  # noqa: ANN401
        counters: DiscoveryCounters,
    ) -> EntryId | None:
        """Classify one entry and record delivered entries still in the source."""
        entry_id = strategy.get_entry_id(entry)
        if entry_id is None:
            counters.missing_id += 1
            logger.warning(
                "Skipping entry without a stable ID in feed %s",
                self.feed_id,
            )
            return None

        is_seen = entry_id in self.seen
        self.seen.add(entry_id)
        has_delivered = entry_id in self.delivered

        if is_seen or has_delivered:
            counters.duplicates += 1
            if has_delivered:
                self.delivered_seen.add(entry_id)
            return None

        if self.uses_monotonic_entry_ids and self.watermark is not None:
            numeric_id = _numeric_entry_id(entry_id)
            if numeric_id is not None and numeric_id <= self.watermark:
                counters.watermark_skipped += 1
                return None

        counters.unhandled += 1
        return entry_id
