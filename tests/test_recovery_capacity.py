from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.price_safety import (
    MAX_PRICE_MANIFEST_ITEMS,
    canonical_manifest_fingerprint,
)
from rss2discord.recovery_models import PriceChangeRecord


def _record(feed_id: str, product_id: str) -> PriceChangeRecord:
    return PriceChangeRecord(
        product_id,
        PriceSnapshot(feed_id, product_id, Decimal(1), "1 EUR", "EUR"),
        PriceSnapshot(feed_id, product_id, Decimal(2), "2 EUR", "EUR"),
    )


def test_near_capacity_manifest_persists_and_loads_with_one_item_query(
    tmp_path: Path,
) -> None:
    records = tuple(
        _record("capacity", str(index)) for index in range(MAX_PRICE_MANIFEST_ITEMS)
    )
    fingerprint = canonical_manifest_fingerprint(
        feed_id="capacity",
        provider="test",
        items=records,
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        batch = store.record_price_change_candidate(
            feed_id="capacity",
            provider="test",
            fingerprint=fingerprint,
            catalog_count=MAX_PRICE_MANIFEST_ITEMS,
            available_count=MAX_PRICE_MANIFEST_ITEMS,
            items=records,
        )
        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)

        loaded = store._load_price_batch(batch.batch_id)

        assert loaded is not None
        assert loaded.item_count == MAX_PRICE_MANIFEST_ITEMS
        assert loaded.items[0].product_id == "0"
        assert loaded.items[-1].product_id == max(
            str(index) for index in range(MAX_PRICE_MANIFEST_ITEMS)
        )
        assert (
            sum(
                statement.lstrip().upper().startswith("SELECT")
                for statement in statements
            )
            == 1
        )


def test_manifest_schema_has_cascades_and_bounded_status_indexes(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        foreign_keys = store._connection.execute("PRAGMA foreign_keys").fetchone()
        indexes = {
            row[1]
            for row in store._connection.execute(
                "PRAGMA index_list(price_change_batches)",
            )
        }
        foreign_key_rows = store._connection.execute(
            "PRAGMA foreign_key_list(price_change_batch_items)",
        ).fetchall()

        assert foreign_keys == (1,)
        assert "price_change_active_one" in indexes
        assert any(row[6] == "CASCADE" for row in foreign_key_rows)
