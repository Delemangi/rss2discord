"""Availability persistence, schema migration, and outbox serialization."""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_schema import initialize_schema
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import entry_from_json, entry_to_json

LEGACY_SNAPSHOTS = (
    "CREATE TABLE price_snapshots (feed_id TEXT NOT NULL, "
    "product_id TEXT NOT NULL, amount TEXT NOT NULL, formatted TEXT NOT NULL, "
    "currency TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT (unixepoch()), "
    "PRIMARY KEY (feed_id, product_id)) WITHOUT ROWID"
)


def snapshot(available: bool | None) -> PriceSnapshot:
    return PriceSnapshot(
        feed_id="feed",
        product_id="1",
        amount=Decimal(100),
        formatted="100 MKD",
        currency="MKD",
        available=available,
    )


def test_fresh_schema_has_an_optional_available_column(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "state.db") as connection:
        initialize_schema(connection)
        columns = {
            str(row[1]): row
            for row in connection.execute("PRAGMA table_info(price_snapshots)")
        }

    assert "available" in columns
    assert columns["available"][3] == 0  # nullable


def test_legacy_price_snapshots_gain_the_available_column(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute(LEGACY_SNAPSHOTS)
        connection.execute(
            "INSERT INTO price_snapshots "
            "(feed_id, product_id, amount, formatted, currency) "
            "VALUES ('feed', '1', '100', '100 MKD', 'MKD')",
        )

    with sqlite3.connect(database) as connection:
        initialize_schema(connection)
        initialize_schema(connection)
        row = connection.execute(
            "SELECT amount, available FROM price_snapshots",
        ).fetchone()

    assert row == ("100", None)


def test_availability_round_trips_through_the_repository(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(
            (
                PriceSnapshot("feed", "1", Decimal(100), "100 MKD", "MKD", True),
                PriceSnapshot("feed", "2", Decimal(100), "100 MKD", "MKD", False),
                PriceSnapshot("feed", "3", Decimal(100), "100 MKD", "MKD"),
            ),
        )

        persisted = {
            item.product_id: item.available
            for item in store.load_price_snapshots("feed")
        }

    assert persisted == {"1": True, "2": False, "3": None}


def test_availability_change_updates_the_snapshot_without_a_history_row(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(snapshot(False))
        before = store._connection.execute(
            "SELECT COUNT(*) FROM price_history",
        ).fetchone()[0]

        store.upsert_price_snapshot(snapshot(True))

        after = store._connection.execute(
            "SELECT COUNT(*) FROM price_history",
        ).fetchone()[0]
        persisted = store.load_price_snapshots("feed")[0]

    assert (before, after) == (1, 1)
    assert persisted.available is True


def test_restocked_survives_the_outbox_json_round_trip() -> None:
    entry = EntryData(
        title="Item",
        link="https://example.test/item",
        description="",
        author="",
        timestamp=None,
        restocked=True,
    )

    assert entry_from_json(entry_to_json(entry)).restocked is True


def test_outbox_json_without_restocked_defaults_to_false() -> None:
    payload = json.loads(
        entry_to_json(
            EntryData(
                title="Item",
                link="https://example.test/item",
                description="",
                author="",
                timestamp=None,
            ),
        ),
    )
    del payload["restocked"]

    restored = entry_from_json(json.dumps(payload))

    assert restored.restocked is False
