"""SQLite schema creation and one-way migrations."""

from __future__ import annotations

import sqlite3
from typing import Final

# Children before parents: SQLite enforces the legacy foreign keys during DROP.
LEGACY_TABLES: Final = (
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


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create current tables and apply idempotent migrations."""
    # Foreign keys are enforced at runtime; disabling them for the one-way
    # legacy-table drop keeps the migration independent of foreign-key
    # ordering in databases created by older releases.
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS delivered_entries (feed_id TEXT NOT NULL, "
            "entry_id TEXT NOT NULL, delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
            "PRIMARY KEY (feed_id, entry_id)) WITHOUT ROWID",
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS initialized_feeds (feed_id TEXT NOT NULL PRIMARY KEY, "
            "initialized_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS discovery_watermarks (feed_id TEXT NOT NULL PRIMARY KEY, "
            "max_entry_id INTEGER NOT NULL, "
            "updated_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS price_snapshots (feed_id TEXT NOT NULL, "
            "product_id TEXT NOT NULL, amount TEXT NOT NULL, formatted TEXT NOT NULL, "
            "currency TEXT NOT NULL, available INTEGER, "
            "updated_at INTEGER NOT NULL DEFAULT (unixepoch()), "
            "last_seen_at INTEGER NOT NULL DEFAULT (unixepoch()), "
            "PRIMARY KEY (feed_id, product_id)) WITHOUT ROWID",
        )
        snapshot_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(price_snapshots)")
        }
        if "available" not in snapshot_columns:
            connection.execute(
                "ALTER TABLE price_snapshots ADD COLUMN available INTEGER",
            )
        if "last_seen_at" not in snapshot_columns:
            connection.execute(
                "ALTER TABLE price_snapshots ADD COLUMN last_seen_at INTEGER",
            )
        connection.execute(
            "UPDATE price_snapshots SET last_seen_at = unixepoch() "
            "WHERE last_seen_at IS NULL",
        )
        connection.execute(
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
            "sent_at INTEGER, last_error TEXT, dead_lettered_at INTEGER)",
        )
        notification_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(price_notifications)",
            )
        }
        for column in ("previous_amount", "current_amount"):
            if column not in notification_columns:
                connection.execute(
                    f"ALTER TABLE price_notifications ADD COLUMN {column} TEXT",
                )
        if "dead_lettered_at" not in notification_columns:
            connection.execute(
                "ALTER TABLE price_notifications ADD COLUMN dead_lettered_at INTEGER",
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS price_notifications_pending "
            "ON price_notifications(feed_id, status, notification_id)",
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS price_notifications_live_pending "
            "ON price_notifications(feed_id, notification_id) "
            "WHERE status = 'pending' AND dead_lettered_at IS NULL",
        )
        legacy = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'anhoch_price_snapshots'",
        ).fetchone()
        if legacy is not None:
            connection.execute(
                "INSERT OR IGNORE INTO price_snapshots "
                "(feed_id, product_id, amount, formatted, currency, updated_at) "
                "SELECT feed_id, CAST(product_id AS TEXT), amount, formatted, currency, updated_at "
                "FROM anhoch_price_snapshots",
            )
            connection.execute("DROP TABLE anhoch_price_snapshots")
        # Price history is append-only. Seed one baseline row per existing
        # snapshot on the first creation only so restarts never resurrect rows
        # that retention or an operator removed.
        price_history_exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'price_history'",
            ).fetchone()
            is not None
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS price_history (feed_id TEXT NOT NULL, "
            "product_id TEXT NOT NULL, amount TEXT NOT NULL, formatted TEXT NOT NULL, "
            "currency TEXT NOT NULL, observed_at INTEGER NOT NULL, "
            "PRIMARY KEY (feed_id, product_id, observed_at)) WITHOUT ROWID",
        )
        if not price_history_exists:
            connection.execute(
                "INSERT OR IGNORE INTO price_history "
                "(feed_id, product_id, amount, formatted, currency, observed_at) "
                "SELECT feed_id, product_id, amount, formatted, currency, updated_at "
                "FROM price_snapshots",
            )
        connection.execute(
            "INSERT OR IGNORE INTO initialized_feeds (feed_id) "
            "SELECT DISTINCT feed_id FROM delivered_entries",
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS price_confirmation_cursors ("
            "feed_id TEXT PRIMARY KEY, last_product_id TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
        )
        legacy_cursor = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'price_normal_delivery_cursors'",
        ).fetchone()
        if legacy_cursor is not None:
            connection.execute(
                "INSERT OR IGNORE INTO price_confirmation_cursors "
                "(feed_id, last_product_id, updated_at) "
                "SELECT feed_id, last_product_id, updated_at "
                "FROM price_normal_delivery_cursors",
            )
            connection.execute("DROP TABLE price_normal_delivery_cursors")
        # Phase 3 intentionally discards legacy approval/hold/reconciliation
        # history. Take a database backup before upgrading if that history
        # must be retained; drops are idempotent.
        for table in LEGACY_TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute(
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
            str(row[1]) for row in connection.execute("PRAGMA table_info(health_state)")
        }
        for column in ("last_success_at", "last_nonempty_at"):
            if column not in health_columns:
                connection.execute(
                    f"ALTER TABLE health_state ADD COLUMN {column} INTEGER",
                )
        connection.execute(
            "UPDATE health_state SET state = 'failed', "
            "cause = COALESCE(cause, 'LegacyState') "
            "WHERE state IN ('unknown', 'quarantined', 'recovery_required')",
        )
    connection.execute("PRAGMA foreign_keys = ON")
