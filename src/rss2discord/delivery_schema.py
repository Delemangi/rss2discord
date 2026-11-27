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
    _configure_connection(connection)
    with connection:
        _create_delivered_entries(connection)
        _create_initialized_feeds(connection)
        _create_discovery_watermarks(connection)
        _create_price_snapshots(connection)
        _create_price_notifications(connection)
        _migrate_legacy_anhoch_snapshots(connection)
        _create_price_history(connection)
        _backfill_initialized_feeds(connection)
        _create_price_confirmation_cursors(connection)
        _migrate_legacy_price_cursors(connection)
        _drop_legacy_tables(connection)
        _create_health_state(connection)
    connection.execute("PRAGMA foreign_keys = ON")


def _configure_connection(connection: sqlite3.Connection) -> None:
    # Foreign keys are enforced at runtime; disabling them for the one-way
    # legacy-table drop keeps the migration independent of foreign-key
    # ordering in databases created by older releases.
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    query = f"PRAGMA table_info({table})"
    return {str(row[1]) for row in connection.execute(query)}


def _create_delivered_entries(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS delivered_entries (feed_id TEXT NOT NULL, "
        "entry_id TEXT NOT NULL, delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
        "PRIMARY KEY (feed_id, entry_id)) WITHOUT ROWID",
    )


def _create_initialized_feeds(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS initialized_feeds (feed_id TEXT NOT NULL PRIMARY KEY, "
        "initialized_at INTEGER NOT NULL DEFAULT (unixepoch()), "
        "entry_id_scheme INTEGER NOT NULL DEFAULT 1) WITHOUT ROWID",
    )
    columns = _table_columns(connection, "initialized_feeds")
    if "entry_id_scheme" not in columns:
        connection.execute(
            "ALTER TABLE initialized_feeds ADD COLUMN "
            "entry_id_scheme INTEGER NOT NULL DEFAULT 1",
        )


def _create_discovery_watermarks(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS discovery_watermarks (feed_id TEXT NOT NULL PRIMARY KEY, "
        "max_entry_id INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
    )


def _create_price_snapshots(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS price_snapshots (feed_id TEXT NOT NULL, "
        "product_id TEXT NOT NULL, amount TEXT NOT NULL, formatted TEXT NOT NULL, "
        "currency TEXT NOT NULL, available INTEGER, "
        "updated_at INTEGER NOT NULL DEFAULT (unixepoch()), "
        "last_seen_at INTEGER NOT NULL DEFAULT (unixepoch()), "
        "PRIMARY KEY (feed_id, product_id)) WITHOUT ROWID",
    )
    columns = _table_columns(connection, "price_snapshots")
    if "available" not in columns:
        connection.execute(
            "ALTER TABLE price_snapshots ADD COLUMN available INTEGER",
        )
    if "last_seen_at" not in columns:
        connection.execute(
            "ALTER TABLE price_snapshots ADD COLUMN last_seen_at INTEGER",
        )
    connection.execute(
        "UPDATE price_snapshots SET last_seen_at = unixepoch() "
        "WHERE last_seen_at IS NULL",
    )


def _create_price_notifications(connection: sqlite3.Connection) -> None:
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
    columns = _table_columns(connection, "price_notifications")
    for column in ("previous_amount", "current_amount"):
        if column not in columns:
            connection.execute(
                f"ALTER TABLE price_notifications ADD COLUMN {column} TEXT",
            )
    if "dead_lettered_at" not in columns:
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


def _migrate_legacy_anhoch_snapshots(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "anhoch_price_snapshots"):
        return
    connection.execute(
        "INSERT OR IGNORE INTO price_snapshots "
        "(feed_id, product_id, amount, formatted, currency, updated_at) "
        "SELECT feed_id, CAST(product_id AS TEXT), amount, formatted, currency, updated_at "
        "FROM anhoch_price_snapshots",
    )
    connection.execute("DROP TABLE anhoch_price_snapshots")


def _create_price_history(connection: sqlite3.Connection) -> None:
    # Price history is append-only. Seed one baseline row per existing
    # snapshot on the first creation only so restarts never resurrect rows
    # that retention or an operator removed.
    history_exists = _table_exists(connection, "price_history")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS price_history (feed_id TEXT NOT NULL, "
        "product_id TEXT NOT NULL, amount TEXT NOT NULL, formatted TEXT NOT NULL, "
        "currency TEXT NOT NULL, observed_at INTEGER NOT NULL, "
        "PRIMARY KEY (feed_id, product_id, observed_at)) WITHOUT ROWID",
    )
    if history_exists:
        return
    connection.execute(
        "INSERT OR IGNORE INTO price_history "
        "(feed_id, product_id, amount, formatted, currency, observed_at) "
        "SELECT feed_id, product_id, amount, formatted, currency, updated_at "
        "FROM price_snapshots",
    )


def _backfill_initialized_feeds(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO initialized_feeds (feed_id) "
        "SELECT DISTINCT feed_id FROM delivered_entries",
    )


def _create_price_confirmation_cursors(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS price_confirmation_cursors ("
        "feed_id TEXT PRIMARY KEY, last_product_id TEXT NOT NULL, "
        "updated_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
    )


def _migrate_legacy_price_cursors(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "price_normal_delivery_cursors"):
        return
    connection.execute(
        "INSERT OR IGNORE INTO price_confirmation_cursors "
        "(feed_id, last_product_id, updated_at) "
        "SELECT feed_id, last_product_id, updated_at "
        "FROM price_normal_delivery_cursors",
    )
    connection.execute("DROP TABLE price_normal_delivery_cursors")


def _drop_legacy_tables(connection: sqlite3.Connection) -> None:
    # Phase 3 intentionally discards legacy approval/hold/reconciliation
    # history. Take a database backup before upgrading if that history
    # must be retained; drops are idempotent.
    for table in LEGACY_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def _create_health_state(connection: sqlite3.Connection) -> None:
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
    columns = _table_columns(connection, "health_state")
    for column in ("last_success_at", "last_nonempty_at"):
        if column not in columns:
            connection.execute(
                f"ALTER TABLE health_state ADD COLUMN {column} INTEGER",
            )
    connection.execute(
        "UPDATE health_state SET state = 'failed', "
        "cause = COALESCE(cause, 'LegacyState') "
        "WHERE state IN ('unknown', 'quarantined', 'recovery_required')",
    )
