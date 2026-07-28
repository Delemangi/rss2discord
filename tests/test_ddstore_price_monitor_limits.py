from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.transports import FeedFetchError, ddstore_price_monitor
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_ddstore_price_monitor import (
    CatalogStub,
    make_feed,
    make_monitor,
    make_product,
)


class RecordingLimitStore(DeliveryStore):
    def __init__(self, database_path: Path) -> None:
        super().__init__(database_path)
        self.requested_limits: list[int | None] = []

    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]:
        self.requested_limits.append(limit)
        return super().load_price_snapshots(feed_id, limit=limit)


def test_scan_accepts_retained_snapshots_at_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        ddstore_price_monitor,
        "MAX_DDSTORE_RETAINED_SNAPSHOTS",
        2,
        raising=False,
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(
            PriceSnapshot("ddstore", "old", Decimal(100), "100 ден.", "MKD"),
        )
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(make_product("new", amount=200),)]),
            store,
            RecordingSender([]),
        )

        # When
        monitor.scan()

        # Then
        assert {
            snapshot.product_id for snapshot in store.load_price_snapshots("ddstore")
        } == {"new", "old"}


def test_scan_rejects_retained_snapshots_over_limit_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        ddstore_price_monitor,
        "MAX_DDSTORE_RETAINED_SNAPSHOTS",
        2,
        raising=False,
    )
    sender = RecordingSender([])

    with RecordingLimitStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(
            (
                PriceSnapshot("ddstore", "old-1", Decimal(100), "100 ден.", "MKD"),
                PriceSnapshot("ddstore", "old-2", Decimal(200), "200 ден.", "MKD"),
            ),
        )
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(make_product("new", amount=300),)]),
            store,
            sender,
        )

        # When / Then
        with pytest.raises(FeedFetchError) as error:
            monitor.scan()

        assert error.value.strategy == "DDStore"
        assert error.value.cause_type == "SnapshotLimitExceeded"
        assert sender.messages == []
        assert {
            snapshot.product_id for snapshot in store.load_price_snapshots("ddstore")
        } == {"old-1", "old-2"}


def test_scan_rejects_oversized_legacy_snapshot_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        ddstore_price_monitor,
        "MAX_DDSTORE_RETAINED_SNAPSHOTS",
        2,
    )

    with RecordingLimitStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(
            PriceSnapshot(
                "ddstore",
                f"old-{index}",
                Decimal(index),
                f"{index} ден.",
                "MKD",
            )
            for index in range(3)
        )
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(make_product("old-0"),)]),
            store,
            RecordingSender([]),
        )

        # When / Then
        with pytest.raises(FeedFetchError, match="SnapshotLimitExceeded"):
            monitor.scan()

        assert store.requested_limits == [3]
        assert len(DeliveryStore.load_price_snapshots(store, "ddstore")) == 3
