"""Phase 3 schema migration and state-cleanup evidence."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryData
from rss2discord.price_notifications import PriceNotification
from rss2discord.recovery_models import HealthUpdate, PriceSnapshot


def _create_legacy_schema(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE delivered_entries (feed_id TEXT NOT NULL, entry_id TEXT NOT NULL, "
            "delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(feed_id, entry_id)) WITHOUT ROWID",
        )
        connection.execute(
            "CREATE TABLE initialized_feeds (feed_id TEXT NOT NULL PRIMARY KEY, "
            "initialized_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
        )
        connection.execute(
            "CREATE TABLE price_snapshots (feed_id TEXT NOT NULL, product_id TEXT NOT NULL, "
            "amount TEXT NOT NULL, formatted TEXT NOT NULL, currency TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(feed_id, product_id)) WITHOUT ROWID",
        )
        connection.execute("PRAGMA foreign_keys = ON")
        # Mirror the production foreign keys so a wrong DROP order fails.
        connection.execute(
            "CREATE TABLE price_change_batches (batch_id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id TEXT NOT NULL)",
        )
        connection.execute(
            "CREATE TABLE price_change_batch_items (batch_id INTEGER NOT NULL "
            "REFERENCES price_change_batches(batch_id), product_id TEXT NOT NULL, "
            "PRIMARY KEY(batch_id, product_id))",
        )
        connection.execute(
            "CREATE TABLE price_reconciliations (reconciliation_fingerprint TEXT PRIMARY KEY, "
            "batch_id INTEGER NOT NULL REFERENCES price_change_batches(batch_id))",
        )
        connection.execute(
            "CREATE TABLE price_reconciliation_items (reconciliation_fingerprint TEXT NOT NULL "
            "REFERENCES price_reconciliations(reconciliation_fingerprint), product_id TEXT NOT NULL, "
            "PRIMARY KEY(reconciliation_fingerprint, product_id))",
        )
        connection.execute(
            "CREATE TABLE price_product_holds (feed_id TEXT NOT NULL, product_id TEXT NOT NULL, "
            "reconciliation_fingerprint TEXT NOT NULL "
            "REFERENCES price_reconciliations(reconciliation_fingerprint), "
            "PRIMARY KEY(feed_id, product_id))",
        )
        connection.execute(
            "CREATE TABLE price_hold_releases (feed_id TEXT NOT NULL, product_id TEXT NOT NULL, "
            "originating_reconciliation_fingerprint TEXT NOT NULL "
            "REFERENCES price_reconciliations(reconciliation_fingerprint), "
            "PRIMARY KEY(feed_id, product_id, originating_reconciliation_fingerprint))",
        )
        connection.execute(
            "CREATE TABLE baseline_candidates (feed_id TEXT PRIMARY KEY)",
        )
        connection.execute(
            "CREATE TABLE baseline_candidate_entries (feed_id TEXT NOT NULL "
            "REFERENCES baseline_candidates(feed_id), entry_id TEXT NOT NULL, "
            "PRIMARY KEY(feed_id, entry_id))",
        )
        connection.execute("CREATE TABLE baseline_states (feed_id TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE baselined_entries (feed_id TEXT NOT NULL "
            "REFERENCES baseline_states(feed_id), entry_id TEXT NOT NULL, "
            "PRIMARY KEY(feed_id, entry_id))",
        )
        connection.execute("INSERT INTO price_change_batches (feed_id) VALUES ('feed')")
        batch_id = connection.execute(
            "SELECT batch_id FROM price_change_batches",
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO price_change_batch_items (batch_id, product_id) VALUES (?, 'p-1')",
            (batch_id,),
        )
        connection.execute(
            "INSERT INTO price_reconciliations (reconciliation_fingerprint, batch_id) VALUES ('fp', ?)",
            (batch_id,),
        )
        connection.execute(
            "INSERT INTO price_reconciliation_items (reconciliation_fingerprint, product_id) "
            "VALUES ('fp', 'p-1')",
        )
        connection.execute(
            "INSERT INTO price_product_holds (feed_id, product_id, reconciliation_fingerprint) "
            "VALUES ('feed', 'p-1', 'fp')",
        )
        connection.execute(
            "INSERT INTO price_hold_releases "
            "(feed_id, product_id, originating_reconciliation_fingerprint) VALUES ('feed', 'p-1', 'fp')",
        )
        connection.execute("INSERT INTO baseline_candidates (feed_id) VALUES ('feed')")
        connection.execute(
            "INSERT INTO baseline_candidate_entries (feed_id, entry_id) VALUES ('feed', 'e-1')",
        )
        connection.execute("INSERT INTO baseline_states (feed_id) VALUES ('feed')")
        connection.execute(
            "INSERT INTO baselined_entries (feed_id, entry_id) VALUES ('feed', 'e-1')",
        )
        connection.execute(
            "CREATE TABLE price_normal_delivery_cursors (feed_id TEXT PRIMARY KEY, "
            "last_product_id TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT (unixepoch())) WITHOUT ROWID",
        )
        connection.execute(
            "INSERT INTO price_normal_delivery_cursors (feed_id, last_product_id) VALUES ('feed', 'p-7')",
        )
        connection.execute(
            "CREATE TABLE health_state (feed_id TEXT NOT NULL, job_kind TEXT NOT NULL, "
            "state TEXT NOT NULL, cause TEXT, attempted_at INTEGER, success INTEGER NOT NULL DEFAULT 0, "
            "nonempty INTEGER NOT NULL DEFAULT 0, item_count INTEGER NOT NULL DEFAULT 0, "
            "duration_ms INTEGER, scheduler_lag_ms INTEGER, transition_at INTEGER, "
            "consecutive_failures INTEGER NOT NULL DEFAULT 0, total_attempts INTEGER NOT NULL DEFAULT 0, "
            "total_successes INTEGER NOT NULL DEFAULT 0, total_nonempty INTEGER NOT NULL DEFAULT 0, "
            "total_items INTEGER NOT NULL DEFAULT 0, blocked_until INTEGER, last_notified_at INTEGER, "
            "PRIMARY KEY(feed_id, job_kind)) WITHOUT ROWID",
        )
        for feed_id, state, cause in (
            ("unknown", "unknown", None),
            ("quarantined", "quarantined", "ApprovalRequired"),
            ("recovery", "recovery_required", "CompleteBaselineRequired"),
            ("healthy", "healthy", None),
        ):
            connection.execute(
                "INSERT INTO health_state (feed_id, job_kind, state, cause) VALUES (?, 'ordinary', ?, ?)",
                (feed_id, state, cause),
            )
        connection.execute(
            "INSERT INTO price_snapshots (feed_id, product_id, amount, formatted, currency) "
            "VALUES ('feed', 'p-1', '10', '10 MKD', 'MKD')",
        )
        connection.commit()


def test_legacy_tables_are_dropped_and_cursor_migrates(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _create_legacy_schema(path)
    with DeliveryStore(path) as store:
        tables = {
            str(row[0])
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
        assert "price_change_batches" not in tables
        assert "price_product_holds" not in tables
        assert "price_reconciliations" not in tables
        assert "baseline_candidates" not in tables
        assert "baseline_states" not in tables
        assert "baselined_entries" not in tables
        assert "price_normal_delivery_cursors" not in tables
        assert "price_confirmation_cursors" in tables
        cursor = store._connection.execute(
            "SELECT last_product_id FROM price_confirmation_cursors WHERE feed_id = 'feed'",
        ).fetchone()
        assert cursor == ("p-7",)
        assert store.load_price_snapshots("feed")[0].formatted == "10 MKD"
        assert store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("p-8", "p-9"),
            limit=1,
        ) == ("p-8",)
    with DeliveryStore(path) as store:
        assert store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("p-8", "p-9"),
            limit=1,
        ) == ("p-9",)


def test_legacy_health_states_are_normalized_to_failed(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _create_legacy_schema(path)
    with DeliveryStore(path) as store:
        health = {record.feed_id: record for record in store.list_health()}
        assert health["unknown"].state == "failed"
        assert health["unknown"].cause == "LegacyState"
        assert health["quarantined"].state == "failed"
        assert health["quarantined"].cause == "ApprovalRequired"
        assert health["recovery"].state == "failed"
        assert health["healthy"].state == "healthy"
        assert health["healthy"].last_success_at is None


def test_job_timing_never_creates_an_unknown_health_row(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_job_timing("feed", "price", 10, 20)
        assert store.list_health("feed") == ()
        store.record_health(
            HealthUpdate(
                feed_id="feed",
                job_kind="price",
                state="failed",
                cause="DeliveryRetryPending",
                attempted_at=100,
                success=False,
                nonempty=False,
                item_count=0,
            ),
        )
        store.record_job_timing("feed", "price", 30, 40)
        health = store.list_health("feed")[0]
        assert health.state == "failed"
        assert health.duration_ms == 30
        assert health.scheduler_lag_ms == 40


def test_health_states_are_limited_to_supported_values(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        for index, state in enumerate(("unknown", "quarantined", "recovery_required")):
            with pytest.raises(ValueError, match="unsupported health state"):
                store.record_health(
                    HealthUpdate(
                        feed_id=f"feed-{index}",
                        job_kind="price",
                        state=state,
                        cause="legacy",
                        attempted_at=100,
                        success=False,
                        nonempty=False,
                        item_count=0,
                    ),
                )
        assert store.list_health() == ()


def test_outbox_rows_survive_legacy_schema_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _create_legacy_schema(path)
    with DeliveryStore(path) as store:
        entry = EntryData("Product", "https://shop/1", "", "", None)
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=100,
            baseline_updates=(
                PriceSnapshot("feed", "p-1", Decimal(10), "10 MKD", "MKD"),
            ),
            notifications=(
                PriceNotification(
                    product_id="p-1",
                    previous=None,
                    current=PriceSnapshot("feed", "p-1", Decimal(10), "10 MKD", "MKD"),
                    entry=entry,
                    source_title="Shop",
                ),
            ),
        )
    with DeliveryStore(path) as store:
        assert store.count_pending_price_notifications("feed") == 1


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _create_legacy_schema(path)
    for _ in range(3):
        with DeliveryStore(path):
            pass
    with sqlite3.connect(path) as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
    assert "price_confirmation_cursors" in names
    assert "price_change_batches" not in names


@pytest.mark.parametrize(
    "status",
    ["pending", "sent"],
)
def test_notification_status_filter_reads_only_matching_rows(
    tmp_path: Path,
    status: str,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        entry = EntryData("Product", "https://shop/1", "", "", None)
        store.record_price_scan(
            feed_id="feed",
            group_id="group",
            observed_at=100,
            baseline_updates=(
                PriceSnapshot("feed", "p-1", Decimal(10), "10 MKD", "MKD"),
            ),
            notifications=(
                PriceNotification(
                    product_id="p-1",
                    previous=None,
                    current=PriceSnapshot("feed", "p-1", Decimal(10), "10 MKD", "MKD"),
                    entry=entry,
                    source_title="Shop",
                ),
            ),
        )
        if status == "sent":
            group = store.load_pending_price_group("feed")
            assert group is not None
            store.mark_price_notifications_sent(
                item.notification_id for item in group[1]
            )
        rows = store.list_price_notifications("feed", status)
        assert len(rows) == 1
        assert rows[0]["status"] == status


def test_initialization_retries_a_transient_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DeliveryStore(tmp_path / "state.db", initialize=False)
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_initialize", flaky)
    try:
        store._initialize_with_retry()
    finally:
        store.close()
    assert len(calls) == 3


def test_initialization_does_not_retry_a_schema_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DeliveryStore(tmp_path / "state.db", initialize=False)
    calls: list[int] = []

    def broken() -> None:
        calls.append(1)
        raise sqlite3.OperationalError("no such table: missing")

    monkeypatch.setattr(store, "_initialize", broken)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            store._initialize_with_retry()
    finally:
        store.close()
    assert len(calls) == 1
