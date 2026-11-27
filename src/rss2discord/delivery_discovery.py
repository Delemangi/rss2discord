"""SQLite persistence for discovery delivered state, baselines, and watermarks."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable


class DiscoveryStateRepository:
    """Own the small state tables used by ordinary discovery scans."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def has_delivered(self, feed_id: str, entry_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM delivered_entries WHERE feed_id = ? AND entry_id = ?",
            (feed_id, entry_id),
        ).fetchone()
        return row is not None

    def load_delivered_entry_ids(self, feed_id: str) -> set[str]:
        """Load one feed's delivered IDs for a scan without per-entry queries."""
        return {
            str(row[0])
            for row in self._connection.execute(
                "SELECT entry_id FROM delivered_entries WHERE feed_id = ?",
                (feed_id,),
            )
        }

    def mark_delivered(self, feed_id: str, entry_id: str) -> None:
        self.mark_delivered_many(feed_id, (entry_id,))

    def mark_delivered_many(
        self,
        feed_id: str,
        entry_ids: Iterable[str],
    ) -> None:
        """Record a confirmed batch of delivered entries idempotently."""
        with self._connection:
            self._connection.executemany(
                "INSERT OR IGNORE INTO delivered_entries (feed_id, entry_id) "
                "VALUES (?, ?)",
                ((feed_id, entry_id) for entry_id in entry_ids),
            )

    def touch_delivered_entries(
        self,
        feed_id: str,
        entry_ids: Iterable[str],
    ) -> None:
        """Refresh retention age for entries still present in the source."""
        materialized = tuple(entry_ids)
        if not materialized:
            return
        now = int(time.time())
        cutoff = now - 30 * 86_400
        with self._connection:
            self._connection.executemany(
                "UPDATE delivered_entries SET delivered_at = ? "
                "WHERE feed_id = ? AND entry_id = ? AND delivered_at < ?",
                ((now, feed_id, entry_id, cutoff) for entry_id in materialized),
            )

    def load_discovery_watermark(self, feed_id: str) -> int | None:
        """Return the highest entry ID already handled by a monotonic feed."""
        row = self._connection.execute(
            "SELECT max_entry_id FROM discovery_watermarks WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def store_discovery_watermark(self, feed_id: str, max_entry_id: int) -> None:
        """Advance a monotonic feed's watermark without ever lowering it."""
        with self._connection:
            self._connection.execute(
                "INSERT INTO discovery_watermarks (feed_id, max_entry_id) "
                "VALUES (?, ?) ON CONFLICT(feed_id) DO UPDATE SET "
                "max_entry_id = MAX(max_entry_id, excluded.max_entry_id), "
                "updated_at = unixepoch()",
                (feed_id, max_entry_id),
            )

    def is_feed_initialized(self, feed_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM initialized_feeds WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        return row is not None

    def seed_feed(
        self,
        feed_id: str,
        entry_ids: Iterable[str],
        *,
        entry_id_scheme: int = 1,
    ) -> bool:
        """Record a baseline for a new feed or a changed entry-ID scheme."""
        with self._connection:
            inserted = self._connection.execute(
                "INSERT OR IGNORE INTO initialized_feeds "
                "(feed_id, entry_id_scheme) VALUES (?, ?)",
                (feed_id, entry_id_scheme),
            ).rowcount
            if inserted == 0:
                row = self._connection.execute(
                    "SELECT entry_id_scheme FROM initialized_feeds WHERE feed_id = ?",
                    (feed_id,),
                ).fetchone()
                if row is None or int(row[0]) == entry_id_scheme:
                    return False
                self._connection.execute(
                    "UPDATE initialized_feeds SET entry_id_scheme = ? "
                    "WHERE feed_id = ?",
                    (entry_id_scheme, feed_id),
                )
            self._connection.executemany(
                "INSERT OR IGNORE INTO delivered_entries (feed_id, entry_id) "
                "VALUES (?, ?)",
                ((feed_id, entry_id) for entry_id in entry_ids),
            )
        return True
