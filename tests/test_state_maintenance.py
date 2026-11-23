import logging
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import STATE_JOURNAL_SIZE_LIMIT_BYTES, DeliveryStore
from rss2discord.main import _prune_state
from rss2discord.models import HealthUpdate, PriceSnapshot
from rss2discord.state_maintenance import StateRetentionReporter


def _health(feed_id: str) -> HealthUpdate:
    return HealthUpdate(
        feed_id=feed_id,
        job_kind="ordinary",
        state="healthy",
        cause=None,
        attempted_at=1,
        success=True,
        nonempty=True,
        item_count=1,
    )


def test_writable_store_caps_the_wal_size(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        limit = store._connection.execute("PRAGMA journal_size_limit").fetchone()

    assert limit == (STATE_JOURNAL_SIZE_LIMIT_BYTES,)


def test_prune_unconfigured_health_keeps_current_feeds(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(_health("current"))
        store.record_health(_health("removed"))

        removed = store.prune_unconfigured_health({"current"})

        assert removed == 1
        assert [record.feed_id for record in store.list_health()] == ["current"]


def test_prune_state_removes_old_delivered_rows(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    cutoff = int(time.time()) - 91 * 86_400
    with DeliveryStore(tmp_path / "state.db") as store:
        store._connection.execute(
            "INSERT INTO delivered_entries (feed_id, entry_id, delivered_at) "
            "VALUES (?, ?, ?)",
            ("feed", "old", cutoff),
        )
        store._connection.commit()

        _prune_state(store, 90)

        assert not store.has_delivered("feed", "old")

    assert "Pruned state older than 90 days" in caplog.text


def _snapshot(product_id: str) -> PriceSnapshot:
    return PriceSnapshot(
        feed_id="feed",
        product_id=product_id,
        amount=Decimal(90),
        formatted="90 MKD",
        currency="MKD",
    )


def test_prune_removes_idle_snapshots_only(tmp_path: Path) -> None:
    old = int(time.time()) - 91 * 86_400
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots((_snapshot("old"), _snapshot("new")))
        with store._connection:
            store._connection.execute(
                "UPDATE price_snapshots SET last_seen_at = ? WHERE product_id = 'old'",
                (old,),
            )
            store._connection.execute("DELETE FROM price_history")
            store._connection.executemany(
                "INSERT INTO price_history "
                "(feed_id, product_id, amount, formatted, currency, observed_at) "
                "VALUES ('feed', ?, '90', '90 MKD', 'MKD', ?)",
                (("old", old), ("new", old)),
            )

        result = store.prune_old_records(retention_days=90)

        remaining = {
            str(row[0])
            for row in store._connection.execute(
                "SELECT product_id FROM price_snapshots",
            )
        }
        history = {
            str(row[0])
            for row in store._connection.execute(
                "SELECT product_id FROM price_history",
            )
        }

    assert result.price_snapshots == 1
    assert result.price_history == 1
    assert remaining == {"new"}
    assert history == {"new"}


def test_snapshot_migration_backfills_last_seen_at(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE price_snapshots ("
            "feed_id TEXT NOT NULL, product_id TEXT NOT NULL, "
            "amount TEXT NOT NULL, formatted TEXT NOT NULL, "
            "currency TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL DEFAULT (unixepoch()), "
            "PRIMARY KEY (feed_id, product_id)) WITHOUT ROWID",
        )
        connection.execute(
            "INSERT INTO price_snapshots "
            "(feed_id, product_id, amount, formatted, currency) "
            "VALUES ('feed', '1', '90', '90 MKD', 'MKD')",
        )

    with DeliveryStore(database) as store:
        last_seen = store._connection.execute(
            "SELECT last_seen_at FROM price_snapshots WHERE product_id = '1'",
        ).fetchone()

    assert last_seen is not None
    assert last_seen[0] is not None
    assert last_seen[0] > 0


def test_periodic_retention_removes_rows_older_than_retention(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    old = int(time.time()) - 91 * 86_400
    with DeliveryStore(database) as store:
        store._connection.execute(
            "INSERT INTO delivered_entries (feed_id, entry_id, delivered_at) "
            "VALUES (?, ?, ?)",
            ("feed", "old", old),
        )
        store._connection.commit()

    reporter = StateRetentionReporter(
        database_path=database,
        retention_days=90,
    )

    assert reporter.prune_now() == 1

    with DeliveryStore(database) as store:
        assert not store.has_delivered("feed", "old")


def test_disabled_periodic_retention_does_not_start_or_prune(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    reporter = StateRetentionReporter(
        database_path=database,
        retention_days=0,
    )

    reporter.start()
    reporter.stop()

    assert reporter.prune_now() == 0
    assert not database.exists()


def test_periodic_retention_failure_is_logged_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(_path: Path, *, initialize: bool = True) -> DeliveryStore:
        del initialize
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "rss2discord.state_maintenance.DeliveryStore",
        fail,
    )
    reporter = StateRetentionReporter(
        database_path=tmp_path / "state.db",
        retention_days=90,
    )

    assert reporter.prune_now() == 0
    assert "Periodic state retention failed" in caplog.text
