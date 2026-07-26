import sqlite3
from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot


def test_price_snapshots_persist_an_arbitrary_string_product_id(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    snapshot = PriceSnapshot(
        feed_id="setec-price-alerts",
        product_id="setec-ABC-123",
        amount=Decimal("1499.00"),
        formatted="1.499 ден.",
        currency="MKD",
    )

    # When
    with DeliveryStore(database_path) as store:
        store.upsert_price_snapshot(snapshot)
        product_ids = store._connection.execute(
            "SELECT product_id FROM price_snapshots",
        ).fetchall()
        snapshots = store.load_price_snapshots("setec-price-alerts")

    # Then
    assert product_ids == [("setec-ABC-123",)]
    assert snapshots == (snapshot,)


def test_delivery_store_migrates_legacy_anhoch_price_snapshots(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    updated_at = 1_735_689_600
    with sqlite3.connect(database_path) as legacy_connection:
        legacy_connection.execute(
            "CREATE TABLE anhoch_price_snapshots ("
            "feed_id TEXT NOT NULL, "
            "product_id INTEGER NOT NULL, "
            "amount TEXT NOT NULL, "
            "formatted TEXT NOT NULL, "
            "currency TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL, "
            "PRIMARY KEY (feed_id, product_id)"
            ") WITHOUT ROWID",
        )
        legacy_connection.execute(
            "INSERT INTO anhoch_price_snapshots "
            "(feed_id, product_id, amount, formatted, currency, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "anhoch-price-alerts",
                9223372036854775807,
                "12.50",
                "12,50 ден.",
                "MKD",
                updated_at,
            ),
        )

    # When
    with DeliveryStore(database_path) as store:
        snapshots = store.load_price_snapshots("anhoch-price-alerts")
        migrated_row = store._connection.execute(
            "SELECT amount, formatted, currency, updated_at FROM price_snapshots",
        ).fetchone()
        table_names = {
            row[0]
            for row in store._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }

    with DeliveryStore(database_path) as reopened_store:
        reopened_snapshots = reopened_store.load_price_snapshots("anhoch-price-alerts")

    # Then
    assert snapshots == (
        PriceSnapshot(
            feed_id="anhoch-price-alerts",
            product_id="9223372036854775807",
            amount=Decimal("12.50"),
            formatted="12,50 ден.",
            currency="MKD",
        ),
    )
    assert migrated_row == ("12.50", "12,50 ден.", "MKD", updated_at)
    assert "price_snapshots" in table_names
    assert "anhoch_price_snapshots" not in table_names
    assert reopened_snapshots == snapshots
