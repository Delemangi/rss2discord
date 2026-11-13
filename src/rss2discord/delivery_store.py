"""SQLite persistence for delivery state, price snapshots, outbox, and health."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self

from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationRecord,
    entry_from_json,
    entry_to_json,
)
from rss2discord.price_safety import HEALTH_REMINDER_SECONDS
from rss2discord.recovery_models import (
    HealthNotice,
    HealthRecord,
    HealthUpdate,
    PriceSnapshot,
)

BEGIN_IMMEDIATE = "BEGIN IMMEDIATE"
FEED_WHERE_CLAUSE = " WHERE feed_id = ?"
MAX_PRICE_NOTIFICATION_ERROR_CHARACTERS = 200
# Children before parents: SQLite enforces the legacy foreign keys during DROP.
LEGACY_TABLES = (
    "price_reconciliation_items",
    "price_hold_releases",
    "price_product_holds",
    "price_reconciliations",
    "price_change_batch_items",
    "price_change_batches",
    "baseline_candidate_entries",
    "baseline_candidates",
    "baselined_entries",
    "baseline_states",
)
HEALTH_STATES = frozenset({"healthy", "empty", "failed", "blocked"})


def sanitize_price_notification_error(error: str | None) -> str | None:
    """Keep only one bounded printable line of a delivery error."""
    if error is None:
        return None
    cleaned = "".join(
        character if character.isprintable() else " " for character in error
    )
    return " ".join(cleaned.split())[:MAX_PRICE_NOTIFICATION_ERROR_CHARACTERS]


def _is_transient_lock(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


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
        self._read_only = read_only
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
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO delivered_entries (feed_id, entry_id) "
                "VALUES (?, ?)",
                (feed_id, entry_id),
            )

    def count_delivered(self, feed_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM delivered_entries WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
        return 0 if row is None else int(row[0])

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
        rows = self._connection.execute(
            "SELECT product_id, amount, formatted, currency "
            "FROM price_snapshots WHERE feed_id = ? ORDER BY product_id LIMIT ?",
            (feed_id, -1 if limit is None else limit),
        )
        return tuple(
            PriceSnapshot(
                feed_id=feed_id,
                product_id=product_id,
                amount=Decimal(amount),
                formatted=formatted,
                currency=currency,
            )
            for product_id, amount, formatted, currency in rows
        )

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        self.upsert_price_snapshots((snapshot,))

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None:
        with self._connection:
            self._connection.executemany(
                "INSERT INTO price_snapshots "
                "(feed_id, product_id, amount, formatted, currency) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(feed_id, product_id) DO UPDATE SET "
                "amount = excluded.amount, formatted = excluded.formatted, "
                "currency = excluded.currency, updated_at = unixepoch() "
                "WHERE price_snapshots.amount <> excluded.amount "
                "OR price_snapshots.formatted <> excluded.formatted "
                "OR price_snapshots.currency <> excluded.currency",
                (
                    (
                        snapshot.feed_id,
                        snapshot.product_id,
                        canonicalize_price_amount(snapshot.amount),
                        snapshot.formatted,
                        snapshot.currency,
                    )
                    for snapshot in snapshots
                ),
            )

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
        """Atomically advance baselines and persist one scan's notifications."""
        baselines = tuple(baseline_updates)
        pending = tuple(notifications)
        if not group_id:
            raise ValueError("notification group ID must be non-empty")
        for snapshot in baselines:
            if snapshot.feed_id != feed_id:
                raise ValueError("baseline feed mismatch")
        for notification in pending:
            if notification.current.feed_id != feed_id:
                raise ValueError("notification feed mismatch")
            if notification.previous is not None and (
                notification.previous.feed_id != feed_id
            ):
                raise ValueError("notification previous feed mismatch")
            if notification.product_id != notification.current.product_id:
                raise ValueError("notification product mismatch")
        with self._connection:
            self._connection.execute(BEGIN_IMMEDIATE)
            try:
                self._connection.executemany(
                    "INSERT INTO price_snapshots "
                    "(feed_id, product_id, amount, formatted, currency) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(feed_id, product_id) DO UPDATE SET "
                    "amount = excluded.amount, formatted = excluded.formatted, "
                    "currency = excluded.currency, updated_at = unixepoch() "
                    "WHERE price_snapshots.amount <> excluded.amount "
                    "OR price_snapshots.formatted <> excluded.formatted "
                    "OR price_snapshots.currency <> excluded.currency",
                    (
                        (
                            snapshot.feed_id,
                            snapshot.product_id,
                            canonicalize_price_amount(snapshot.amount),
                            snapshot.formatted,
                            snapshot.currency,
                        )
                        for snapshot in baselines
                    ),
                )
                self._connection.executemany(
                    "INSERT INTO price_notifications "
                    "(feed_id, group_id, product_id, title, link, previous_price, "
                    "current_price, previous_amount, current_amount, currency, "
                    "entry_json, source_title, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            feed_id,
                            group_id,
                            notification.product_id,
                            notification.entry.title,
                            notification.entry.link,
                            (
                                None
                                if notification.previous is None
                                else notification.previous.formatted
                            ),
                            notification.current.formatted,
                            (
                                None
                                if notification.previous is None
                                else canonicalize_price_amount(
                                    notification.previous.amount,
                                )
                            ),
                            canonicalize_price_amount(notification.current.amount),
                            notification.current.currency,
                            entry_to_json(notification.entry),
                            notification.source_title,
                            observed_at,
                        )
                        for notification in pending
                    ),
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
        return len(pending)

    def load_pending_price_group(
        self,
        feed_id: str,
    ) -> tuple[str, tuple[PriceNotificationRecord, ...]] | None:
        """Load the oldest still-pending group for one feed."""
        row = self._connection.execute(
            "SELECT group_id FROM price_notifications "
            "WHERE feed_id = ? AND status = 'pending' "
            "ORDER BY notification_id LIMIT 1",
            (feed_id,),
        ).fetchone()
        if row is None:
            return None
        group_id = str(row[0])
        records = tuple(
            PriceNotificationRecord(
                notification_id=int(item[0]),
                feed_id=str(item[1]),
                group_id=str(item[2]),
                product_id=str(item[3]),
                title=str(item[4]),
                link=str(item[5]),
                previous_price=None if item[6] is None else str(item[6]),
                current_price=str(item[7]),
                previous_amount=(None if item[8] is None else Decimal(str(item[8]))),
                current_amount=Decimal(str(item[9])),
                currency=str(item[10]),
                entry=entry_from_json(str(item[11])),
                source_title=str(item[12]),
                observed_at=int(item[13]),
                attempts=int(item[14]),
            )
            for item in self._connection.execute(
                "SELECT notification_id, feed_id, group_id, product_id, title, link, "
                "previous_price, current_price, previous_amount, current_amount, "
                "currency, entry_json, source_title, "
                "observed_at, attempts FROM price_notifications "
                "WHERE feed_id = ? AND group_id = ? AND status = 'pending' "
                "ORDER BY notification_id",
                (feed_id, group_id),
            )
        )
        return group_id, records

    def mark_price_notifications_sent(
        self,
        notification_ids: Iterable[int],
    ) -> None:
        ids = tuple(notification_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._connection:
            self._connection.execute(
                "UPDATE price_notifications SET status = 'sent', sent_at = unixepoch() "  # noqa: S608
                f"WHERE status = 'pending' AND notification_id IN ({placeholders})",
                ids,
            )

    def record_price_notification_attempt(
        self,
        notification_ids: Iterable[int],
        error: str | None,
    ) -> None:
        ids = tuple(notification_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._connection:
            self._connection.execute(
                "UPDATE price_notifications "  # noqa: S608
                "SET attempts = attempts + 1, last_attempt_at = unixepoch(), "
                f"last_error = ? WHERE notification_id IN ({placeholders})",
                (sanitize_price_notification_error(error), *ids),
            )

    def count_pending_price_notifications(self, feed_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM price_notifications "
            "WHERE feed_id = ? AND status = 'pending'",
            (feed_id,),
        ).fetchone()
        return 0 if row is None else int(row[0])

    def list_price_notifications(
        self,
        feed_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        """Return outbox rows for read-only inspection."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if status not in {None, "pending", "sent"}:
            raise ValueError("status must be pending, sent, or omitted")
        where: list[str] = []
        parameters: list[object] = []
        if feed_id is not None:
            where.append("feed_id = ?")
            parameters.append(feed_id)
        if status is not None:
            where.append("status = ?")
            parameters.append(status)
        query = (
            "SELECT notification_id, feed_id, group_id, title, previous_price, "
            "current_price, currency, status, attempts, observed_at, sent_at, last_error "
            "FROM price_notifications"
        )
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY feed_id, notification_id LIMIT ?"
        parameters.append(limit)
        return tuple(
            {
                "id": int(row[0]),
                "feed_id": row[1],
                "group_id": row[2],
                "title": row[3],
                "previous_price": row[4],
                "current_price": row[5],
                "currency": row[6],
                "status": row[7],
                "attempts": int(row[8]),
                "observed_at": row[9],
                "sent_at": row[10],
                "last_error": row[11],
            }
            for row in self._connection.execute(query, parameters)
        )

    # Confirmation rotation ----------------------------------------------

    def select_price_confirmation_subset(
        self,
        *,
        feed_id: str,
        product_ids: Iterable[str],
        limit: int,
    ) -> tuple[str, ...]:
        """Rotate a confirmation subset using the durable cursor."""
        if limit < 1:
            raise ValueError("limit must be positive")
        normalized = tuple(sorted(set(product_ids)))
        if any(not product_id for product_id in normalized):
            raise ValueError("price confirmation product IDs must be non-empty")
        with self._connection:
            self._connection.execute(BEGIN_IMMEDIATE)
            try:
                if not normalized:
                    self._connection.commit()
                    return ()
                cursor = self._connection.execute(
                    "SELECT last_product_id FROM price_confirmation_cursors "
                    "WHERE feed_id = ?",
                    (feed_id,),
                ).fetchone()
                last_id = None if cursor is None else str(cursor[0])
                after = (
                    tuple(
                        product_id for product_id in normalized if product_id > last_id
                    )
                    if last_id is not None
                    else normalized
                )
                before = (
                    tuple(
                        product_id for product_id in normalized if product_id <= last_id
                    )
                    if last_id is not None
                    else ()
                )
                selected = (after + before)[:limit] if after else normalized[:limit]
                self._connection.execute(
                    "INSERT INTO price_confirmation_cursors "
                    "(feed_id, last_product_id, updated_at) VALUES (?, ?, unixepoch()) "
                    "ON CONFLICT(feed_id) DO UPDATE SET last_product_id = excluded.last_product_id, "
                    "updated_at = excluded.updated_at",
                    (feed_id, selected[-1]),
                )
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
                return selected

    # Health --------------------------------------------------------------

    def record_health(
        self,
        update: HealthUpdate,
        *,
        reminder_seconds: int = HEALTH_REMINDER_SECONDS,
    ) -> HealthNotice:
        if update.state not in HEALTH_STATES:
            raise ValueError("unsupported health state")
        if reminder_seconds < 0:
            raise ValueError("reminder_seconds must be non-negative")
        with self._connection:
            row = self._connection.execute(
                "SELECT state, last_notified_at, blocked_until "
                "FROM health_state WHERE feed_id = ? AND job_kind = ?",
                (update.feed_id, update.job_kind),
            ).fetchone()
            previous_state = None if row is None else str(row[0])
            previous_notified = None if row is None else row[1]
            previous_blocked_until = None if row is None else row[2]
            changed = previous_state != update.state
            recovered = previous_state in {
                "failed",
                "blocked",
            } and update.state not in {
                "failed",
                "blocked",
            }
            should_log = self._should_log_health(
                update,
                changed,
                recovered,
                previous_notified,
                reminder_seconds,
            )
            blocked_until = previous_blocked_until
            if update.state == "blocked" and (
                previous_state != "blocked"
                or previous_blocked_until is None
                or previous_blocked_until <= update.attempted_at
            ):
                blocked_until = update.attempted_at + HEALTH_REMINDER_SECONDS
            if update.state != "blocked" and changed:
                blocked_until = None
            if row is None:
                self._insert_health_state(update, blocked_until, should_log)
            else:
                self._connection.execute(
                    "UPDATE health_state SET state = ?, cause = ?, attempted_at = ?, "
                    "success = ?, nonempty = ?, item_count = ?, duration_ms = ?, "
                    "scheduler_lag_ms = ?, transition_at = CASE WHEN ? THEN ? ELSE transition_at END, "
                    "consecutive_failures = CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END, "
                    "total_attempts = total_attempts + 1, total_successes = total_successes + ?, "
                    "total_nonempty = total_nonempty + ?, total_items = total_items + ?, "
                    "blocked_until = ?, last_notified_at = CASE WHEN ? THEN ? ELSE last_notified_at END, "
                    "last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, "
                    "last_nonempty_at = CASE WHEN ? THEN ? ELSE last_nonempty_at END "
                    "WHERE feed_id = ? AND job_kind = ?",
                    (
                        update.state,
                        update.cause,
                        update.attempted_at,
                        int(update.success),
                        int(update.nonempty),
                        update.item_count,
                        update.duration_ms,
                        update.scheduler_lag_ms,
                        int(changed),
                        update.attempted_at,
                        int(update.success),
                        int(update.success),
                        int(update.nonempty),
                        update.item_count,
                        blocked_until,
                        int(should_log),
                        update.attempted_at,
                        int(update.success),
                        update.attempted_at,
                        int(update.nonempty),
                        update.attempted_at,
                        update.feed_id,
                        update.job_kind,
                    ),
                )
        return HealthNotice(changed, recovered, should_log, previous_state)

    def _insert_health_state(
        self,
        update: HealthUpdate,
        blocked_until: int | None,
        should_log: bool,
    ) -> None:
        self._connection.execute(
            "INSERT INTO health_state "
            "(feed_id, job_kind, state, cause, attempted_at, success, nonempty, "
            "item_count, duration_ms, scheduler_lag_ms, transition_at, "
            "consecutive_failures, total_attempts, total_successes, total_nonempty, "
            "total_items, blocked_until, last_notified_at, last_success_at, "
            "last_nonempty_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (
                update.feed_id,
                update.job_kind,
                update.state,
                update.cause,
                update.attempted_at,
                int(update.success),
                int(update.nonempty),
                update.item_count,
                update.duration_ms,
                update.scheduler_lag_ms,
                update.attempted_at,
                0 if update.success else 1,
                int(update.success),
                int(update.nonempty),
                update.item_count,
                blocked_until,
                update.attempted_at if should_log else None,
                update.attempted_at if update.success else None,
                update.attempted_at if update.nonempty else None,
            ),
        )

    @staticmethod
    def _should_log_health(
        update: HealthUpdate,
        changed: bool,
        recovered: bool,
        previous_notified: int | None,
        reminder_seconds: int,
    ) -> bool:
        return (
            changed
            or recovered
            or (
                update.state in {"failed", "blocked"}
                and (
                    previous_notified is None
                    or update.attempted_at - int(previous_notified) >= reminder_seconds
                )
            )
        )

    def get_blocked_until(self, feed_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT MAX(blocked_until) FROM health_state "
            "WHERE feed_id = ? AND blocked_until IS NOT NULL AND blocked_until > unixepoch()",
            (feed_id,),
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def record_job_timing(
        self,
        feed_id: str,
        job_kind: str,
        duration_ms: int,
        scheduler_lag_ms: int,
    ) -> None:
        """Update timing columns on an existing health row only."""
        with self._connection:
            self._connection.execute(
                "UPDATE health_state SET duration_ms = ?, scheduler_lag_ms = ? "
                "WHERE feed_id = ? AND job_kind = ?",
                (duration_ms, scheduler_lag_ms, feed_id, job_kind),
            )

    def list_health(self, feed_id: str | None = None) -> tuple[HealthRecord, ...]:
        where = FEED_WHERE_CLAUSE if feed_id is not None else ""
        query = (
            "SELECT feed_id, job_kind, state, cause, attempted_at, success, nonempty, "
            "item_count, duration_ms, scheduler_lag_ms, transition_at, "
            "consecutive_failures, total_attempts, total_successes, total_nonempty, "
            "total_items, blocked_until, last_notified_at, last_success_at, "
            "last_nonempty_at FROM health_state"
        )
        query += where
        query += " ORDER BY feed_id, job_kind"
        rows = self._connection.execute(
            query,
            () if feed_id is None else (feed_id,),
        )
        return tuple(
            HealthRecord(
                feed_id=row[0],
                job_kind=row[1],
                state=row[2],
                cause=row[3],
                attempted_at=row[4],
                success=bool(row[5]),
                nonempty=bool(row[6]),
                item_count=int(row[7]),
                duration_ms=row[8],
                scheduler_lag_ms=row[9],
                transition_at=row[10],
                consecutive_failures=int(row[11]),
                total_attempts=int(row[12]),
                total_successes=int(row[13]),
                total_nonempty=int(row[14]),
                total_items=int(row[15]),
                blocked_until=row[16],
                last_notified_at=row[17],
                last_success_at=row[18],
                last_nonempty_at=row[19],
            )
            for row in rows
        )

    # SQLite schema and migrations ---------------------------------------

    def _initialize(self) -> None:
        # Foreign keys are enforced at runtime; disabling them for the one-way
        # legacy-table drop keeps the migration independent of foreign-key
        # ordering in databases created by older releases.
        self._connection.execute("PRAGMA foreign_keys = OFF")
        self._connection.execute("PRAGMA temp_store = MEMORY")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS delivered_entries (feed_id TEXT NOT NULL, "
                "entry_id TEXT NOT NULL, delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "PRIMARY KEY (feed_id, entry_id)) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS initialized_feeds (feed_id TEXT NOT NULL PRIMARY KEY, "
                "initialized_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_snapshots (feed_id TEXT NOT NULL, "
                "product_id TEXT NOT NULL, amount TEXT NOT NULL, formatted TEXT NOT NULL, "
                "currency TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT (unixepoch()), "
                "PRIMARY KEY (feed_id, product_id)) WITHOUT ROWID",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_notifications ("
                "notification_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "feed_id TEXT NOT NULL, group_id TEXT NOT NULL, product_id TEXT NOT NULL, "
                "title TEXT NOT NULL, link TEXT NOT NULL, previous_price TEXT, "
                "current_price TEXT NOT NULL, previous_amount TEXT, "
                "current_amount TEXT, currency TEXT NOT NULL, "
                "entry_json TEXT NOT NULL, source_title TEXT NOT NULL, "
                "observed_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending' "
                "CHECK(status IN ('pending','sent')), "
                "attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at INTEGER, "
                "sent_at INTEGER, last_error TEXT)",
            )
            notification_columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(price_notifications)",
                )
            }
            for column in ("previous_amount", "current_amount"):
                if column not in notification_columns:
                    self._connection.execute(
                        f"ALTER TABLE price_notifications ADD COLUMN {column} TEXT",
                    )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS price_notifications_pending "
                "ON price_notifications(feed_id, status, notification_id)",
            )
            legacy = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'anhoch_price_snapshots'",
            ).fetchone()
            if legacy is not None:
                self._connection.execute(
                    "INSERT OR IGNORE INTO price_snapshots "
                    "(feed_id, product_id, amount, formatted, currency, updated_at) "
                    "SELECT feed_id, CAST(product_id AS TEXT), amount, formatted, currency, updated_at "
                    "FROM anhoch_price_snapshots",
                )
                self._connection.execute("DROP TABLE anhoch_price_snapshots")
            self._connection.execute(
                "INSERT OR IGNORE INTO initialized_feeds (feed_id) "
                "SELECT DISTINCT feed_id FROM delivered_entries",
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS price_confirmation_cursors ("
                "feed_id TEXT PRIMARY KEY, last_product_id TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
            )
            legacy_cursor = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'price_normal_delivery_cursors'",
            ).fetchone()
            if legacy_cursor is not None:
                self._connection.execute(
                    "INSERT OR IGNORE INTO price_confirmation_cursors "
                    "(feed_id, last_product_id, updated_at) "
                    "SELECT feed_id, last_product_id, updated_at "
                    "FROM price_normal_delivery_cursors",
                )
                self._connection.execute("DROP TABLE price_normal_delivery_cursors")
            # Phase 3 intentionally discards legacy approval/hold/reconciliation
            # history. Take a database backup before upgrading if that history
            # must be retained; drops are idempotent.
            for table in LEGACY_TABLES:
                self._connection.execute(f"DROP TABLE IF EXISTS {table}")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS health_state (feed_id TEXT NOT NULL, job_kind TEXT NOT NULL, "
                "state TEXT NOT NULL, cause TEXT, attempted_at INTEGER, success INTEGER NOT NULL DEFAULT 0, "
                "nonempty INTEGER NOT NULL DEFAULT 0, item_count INTEGER NOT NULL DEFAULT 0, "
                "duration_ms INTEGER, scheduler_lag_ms INTEGER, transition_at INTEGER, "
                "consecutive_failures INTEGER NOT NULL DEFAULT 0, total_attempts INTEGER NOT NULL DEFAULT 0, "
                "total_successes INTEGER NOT NULL DEFAULT 0, total_nonempty INTEGER NOT NULL DEFAULT 0, "
                "total_items INTEGER NOT NULL DEFAULT 0, blocked_until INTEGER, last_notified_at INTEGER, "
                "last_success_at INTEGER, last_nonempty_at INTEGER, "
                "PRIMARY KEY(feed_id, job_kind)) WITHOUT ROWID",
            )
            health_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(health_state)")
            }
            for column in ("last_success_at", "last_nonempty_at"):
                if column not in health_columns:
                    self._connection.execute(
                        f"ALTER TABLE health_state ADD COLUMN {column} INTEGER",
                    )
            self._connection.execute(
                "UPDATE health_state SET state = 'failed', "
                "cause = COALESCE(cause, 'LegacyState') "
                "WHERE state IN ('unknown', 'quarantined', 'recovery_required')",
            )
        self._connection.execute("PRAGMA foreign_keys = ON")
