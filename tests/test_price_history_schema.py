"""Price-history schema creation, first-creation backfill, and idempotence."""

import sqlite3
from pathlib import Path

from rss2discord.delivery_schema import initialize_schema

EXPECTED_COLUMNS = (
    "feed_id",
    "product_id",
    "amount",
    "formatted",
    "currency",
    "observed_at",
)
INSERT_SNAPSHOT = (
    "INSERT INTO price_snapshots "
    "(feed_id, product_id, amount, formatted, currency, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_HISTORY = (
    "INSERT INTO price_history "
    "(feed_id, product_id, amount, formatted, currency, observed_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
INSERT_HISTORY_IGNORE = INSERT_HISTORY.replace("INSERT INTO", "INSERT OR IGNORE INTO")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _history_rows(
    connection: sqlite3.Connection,
) -> list[tuple[str, str, str, str, str, int]]:
    return [
        (
            str(feed_id),
            str(product_id),
            str(amount),
            str(formatted),
            str(currency),
            int(observed_at),
        )
        for feed_id, product_id, amount, formatted, currency, observed_at in (
            connection.execute(
                "SELECT feed_id, product_id, amount, formatted, currency, observed_at "
                "FROM price_history ORDER BY feed_id, product_id, observed_at",
            )
        )
    ]


def test_fresh_schema_creates_an_empty_price_history_table(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "state.db") as connection:
        initialize_schema(connection)

        assert _table_exists(connection, "price_history")
        assert _history_rows(connection) == []
        columns = connection.execute("PRAGMA table_info(price_history)").fetchall()
        assert tuple(str(row[1]) for row in columns) == EXPECTED_COLUMNS
        assert tuple(str(row[1]) for row in columns if row[5]) == (
            "feed_id",
            "product_id",
            "observed_at",
        )
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'price_history'",
        ).fetchone()[0]
        assert "WITHOUT ROWID" in str(sql)


def test_first_initialization_backfills_one_baseline_per_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        initialize_schema(connection)
        connection.execute("DROP TABLE price_history")
        connection.executemany(
            INSERT_SNAPSHOT,
            (
                ("feed-a", "1", "100", "100 MKD", "MKD", 1_700_000_000),
                ("feed-b", "2", "50", "50 EUR", "EUR", 1_700_000_500),
            ),
        )

    with sqlite3.connect(database) as connection:
        initialize_schema(connection)
        assert _history_rows(connection) == [
            ("feed-a", "1", "100", "100 MKD", "MKD", 1_700_000_000),
            ("feed-b", "2", "50", "50 EUR", "EUR", 1_700_000_500),
        ]


def test_reinitialization_neither_duplicates_nor_resurrects_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        initialize_schema(connection)
        connection.execute("DROP TABLE price_history")
        connection.execute(
            INSERT_SNAPSHOT,
            ("feed", "1", "100", "100 MKD", "MKD", 1_700_000_000),
        )

    with sqlite3.connect(database) as connection:
        initialize_schema(connection)
        initialize_schema(connection)
        assert len(_history_rows(connection)) == 1

        connection.execute("DELETE FROM price_history")
        connection.commit()

    with sqlite3.connect(database) as connection:
        initialize_schema(connection)
        assert _history_rows(connection) == []


def test_primary_key_rejects_a_duplicate_observation(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "state.db") as connection:
        initialize_schema(connection)
        row = ("feed", "1", "100", "100 MKD", "MKD", 1_700_000_000)
        connection.execute(INSERT_HISTORY, row)
        connection.execute(INSERT_HISTORY_IGNORE, row)
        assert len(_history_rows(connection)) == 1

        try:
            connection.execute(INSERT_HISTORY, row)
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - the primary key must reject duplicates
            raise AssertionError("duplicate observation was accepted")
