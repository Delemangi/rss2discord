from decimal import Decimal
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot


def test_price_snapshot_loading_stops_at_requested_limit(tmp_path: Path) -> None:
    # Given
    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(
            PriceSnapshot(
                "ddstore",
                f"product-{index}",
                Decimal(index),
                f"{index} ден.",
                "MKD",
            )
            for index in range(4)
        )

        # When
        snapshots = store.load_price_snapshots("ddstore", limit=3)

        # Then
        assert [snapshot.product_id for snapshot in snapshots] == [
            "product-0",
            "product-1",
            "product-2",
        ]
