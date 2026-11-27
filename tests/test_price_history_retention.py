"""Price-history retention keeps history for products that are still observed."""

import time
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore

RETENTION_DAYS = 90
DAY = 86_400
NOW = int(time.time())
OLD = NOW - (RETENTION_DAYS + 30) * DAY

INSERT_HISTORY = (
    "INSERT INTO price_history "
    "(feed_id, product_id, amount, formatted, currency, observed_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_SNAPSHOT = (
    "INSERT INTO price_snapshots "
    "(feed_id, product_id, amount, formatted, currency, last_seen_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def insert_history(
    store: DeliveryStore,
    product_id: str,
    amount: str,
    observed_at: int,
) -> None:
    with store._connection:
        store._connection.execute(
            INSERT_HISTORY,
            ("feed", product_id, amount, f"{amount} MKD", "MKD", observed_at),
        )


def insert_snapshot(
    store: DeliveryStore,
    product_id: str,
    last_seen_at: int,
) -> None:
    with store._connection:
        store._connection.execute(
            INSERT_SNAPSHOT,
            ("feed", product_id, "100", "100 MKD", "MKD", last_seen_at),
        )


def history_rows(store: DeliveryStore) -> list[tuple[str, int]]:
    return [
        (str(product_id), int(observed_at))
        for product_id, observed_at in store._connection.execute(
            "SELECT product_id, observed_at FROM price_history "
            "ORDER BY product_id, observed_at",
        )
    ]


def test_old_history_for_observed_products_is_kept(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        insert_history(store, "1", "100", OLD - 100)
        insert_history(store, "1", "90", OLD)
        insert_history(store, "1", "80", NOW - DAY)
        insert_snapshot(store, "1", NOW - DAY)

        result = store.prune_old_records(retention_days=RETENTION_DAYS)

        assert result.price_history == 0
        assert result.total == 0
        assert history_rows(store) == [
            ("1", OLD - 100),
            ("1", OLD),
            ("1", NOW - DAY),
        ]


def test_history_for_idle_products_is_pruned_with_the_snapshot(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        insert_history(store, "2", "50", OLD - 200)
        insert_history(store, "2", "40", OLD - 100)
        insert_snapshot(store, "2", OLD - 100)

        result = store.prune_old_records(retention_days=RETENTION_DAYS)

        assert result.price_history == 2
        assert result.price_snapshots == 1
        assert history_rows(store) == []
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM price_snapshots",
            ).fetchone()[0]
            == 0
        )


def test_recent_history_is_untouched(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        insert_history(store, "1", "100", NOW - DAY)
        insert_history(store, "1", "90", NOW)
        insert_snapshot(store, "1", NOW)

        result = store.prune_old_records(retention_days=RETENTION_DAYS)

        assert result.price_history == 0
        assert result.total == 0
        assert history_rows(store) == [("1", NOW - DAY), ("1", NOW)]


def test_zero_day_retention_is_a_no_op(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        insert_history(store, "1", "100", OLD - 100)
        insert_history(store, "1", "90", OLD)
        insert_snapshot(store, "1", OLD)

        result = store.prune_old_records(retention_days=0)

        assert result.price_history == 0
        assert result.total == 0
        assert history_rows(store) == [("1", OLD - 100), ("1", OLD)]
