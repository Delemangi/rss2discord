"""SQLite persistence for delivery state, price snapshots, outbox, and health."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from rss2discord.delivery_health import HealthRepository
from rss2discord.delivery_notifications import PriceNotificationRepository
from rss2discord.delivery_prices import PriceSnapshotRepository
from rss2discord.delivery_schema import initialize_schema
from rss2discord.models import (
    HEALTH_REMINDER_SECONDS,
    HealthNotice,
    HealthRecord,
    HealthUpdate,
    PriceSnapshot,
)
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationRecord,
)


def _is_transient_lock(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


@dataclass(frozen=True, slots=True)
class RetentionResult:
    """Rows removed by one state-retention pass."""

    delivered_entries: int = 0
    price_notifications: int = 0

    @property
    def total(self) -> int:
        return self.delivered_entries + self.price_notifications


class DeliveryStore:
    """Own delivery state, price snapshots, the outbox, and health."""

    def __init__(
        self,
        database_path: Path,
        *,
        read_only: bool = False,
        initialize: bool = True,
    ) -> None:
        self._database_path = database_path
        if read_only:
            self._connection = sqlite3.connect(
                database_path.resolve().as_uri() + "?mode=ro",
                uri=True,
            )
        else:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(database_path)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        try:
            if not read_only and initialize:
                self._initialize_with_retry()
        except sqlite3.Error:
            self._connection.close()
            raise
        self._prices = PriceSnapshotRepository(self._connection)
        self._notifications = PriceNotificationRepository(self._connection)
        self._health = HealthRepository(self._connection)

    def _initialize_with_retry(self) -> None:
        """Retry a transient lock; a real schema or I/O failure still escapes."""
        for attempt in range(3):
            try:
                self._initialize()
            except sqlite3.OperationalError as error:
                if attempt == 2 or not _is_transient_lock(error):
                    raise
                time.sleep(0.5 * (attempt + 1))
            else:
                return

    @property
    def database_path(self) -> Path:
        return self._database_path

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    # Ordinary delivery and initialization --------------------------------

    def has_delivered(self, feed_id: str, entry_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM delivered_entries WHERE feed_id = ? AND entry_id = ?",
            (feed_id, entry_id),
        ).fetchone()
        return row is not None

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

    def is_feed_initialized(self, feed_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM initialized_feeds WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        return row is not None

    def seed_feed(self, feed_id: str, entry_ids: Iterable[str]) -> bool:
        """Atomically record existing entries on the first sync only."""
        with self._connection:
            initialized = self._connection.execute(
                "INSERT OR IGNORE INTO initialized_feeds (feed_id) VALUES (?)",
                (feed_id,),
            ).rowcount
            if initialized == 0:
                return False
            self._connection.executemany(
                "INSERT OR IGNORE INTO delivered_entries (feed_id, entry_id) "
                "VALUES (?, ?)",
                ((feed_id, entry_id) for entry_id in entry_ids),
            )
        return True

    # Price snapshots -----------------------------------------------------

    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]:
        return self._prices.load_price_snapshots(feed_id, limit=limit)

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._prices.upsert_price_snapshot(snapshot)

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None:
        self._prices.upsert_price_snapshots(snapshots)

    # Durable price-notification outbox ----------------------------------

    def record_price_scan(
        self,
        *,
        feed_id: str,
        group_id: str,
        observed_at: int,
        baseline_updates: Iterable[PriceSnapshot],
        notifications: Iterable[PriceNotification],
    ) -> int:
        return self._notifications.record_price_scan(
            feed_id=feed_id,
            group_id=group_id,
            observed_at=observed_at,
            baseline_updates=baseline_updates,
            notifications=notifications,
        )

    def load_pending_price_group(
        self,
        feed_id: str,
    ) -> tuple[str, tuple[PriceNotificationRecord, ...]] | None:
        return self._notifications.load_pending_price_group(feed_id)

    def mark_price_notifications_sent(
        self,
        notification_ids: Iterable[int],
    ) -> None:
        self._notifications.mark_price_notifications_sent(notification_ids)

    def record_price_notification_attempt(
        self,
        notification_ids: Iterable[int],
        error: str | None,
    ) -> None:
        self._notifications.record_price_notification_attempt(
            notification_ids,
            error,
        )

    def count_pending_price_notifications(self, feed_id: str) -> int:
        return self._notifications.count_pending_price_notifications(feed_id)

    def list_price_notifications(
        self,
        feed_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        return self._notifications.list_price_notifications(
            feed_id,
            status,
            limit,
        )

    # Confirmation rotation ----------------------------------------------

    def select_price_confirmation_subset(
        self,
        *,
        feed_id: str,
        product_ids: Iterable[str],
        limit: int,
    ) -> tuple[str, ...]:
        return self._notifications.select_price_confirmation_subset(
            feed_id=feed_id,
            product_ids=product_ids,
            limit=limit,
        )

    # Health --------------------------------------------------------------

    def record_health(
        self,
        update: HealthUpdate,
        *,
        reminder_seconds: int = HEALTH_REMINDER_SECONDS,
    ) -> HealthNotice:
        return self._health.record_health(
            update,
            reminder_seconds=reminder_seconds,
        )

    def get_blocked_until(self, feed_id: str) -> int | None:
        return self._health.get_blocked_until(feed_id)

    def record_job_timing(
        self,
        feed_id: str,
        job_kind: str,
        duration_ms: int,
        scheduler_lag_ms: int,
    ) -> None:
        self._health.record_job_timing(
            feed_id,
            job_kind,
            duration_ms,
            scheduler_lag_ms,
        )

    def list_health(self, feed_id: str | None = None) -> tuple[HealthRecord, ...]:
        return self._health.list_health(feed_id)

    # Maintenance ---------------------------------------------------------

    def prune_old_records(self, *, retention_days: int) -> RetentionResult:
        """Delete delivered entries and sent notifications outside the window."""
        if retention_days <= 0:
            return RetentionResult()
        cutoff = int(time.time()) - retention_days * 86_400
        with self._connection:
            delivered = self._connection.execute(
                "DELETE FROM delivered_entries WHERE delivered_at < ?",
                (cutoff,),
            ).rowcount
            notifications = self._connection.execute(
                "DELETE FROM price_notifications WHERE status = 'sent' "
                "AND COALESCE(sent_at, last_attempt_at, observed_at) < ?",
                (cutoff,),
            ).rowcount
        return RetentionResult(
            delivered_entries=max(0, delivered),
            price_notifications=max(0, notifications),
        )

    def vacuum(self) -> None:
        """Reclaim free pages after pruning; call before other writers start."""
        self._connection.execute("VACUUM")

    # SQLite schema and migrations ---------------------------------------

    def _initialize(self) -> None:
        initialize_schema(self._connection)
