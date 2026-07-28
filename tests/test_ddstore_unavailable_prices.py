from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import SourceMetric
from rss2discord.transports import ddstore_price_monitor
from rss2discord.transports.ddstore import DDStoreStrategy
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_ddstore_price_monitor import (
    CatalogStub,
    make_feed,
    make_monitor,
    make_product,
)


def test_ddstore_strategy_labels_zero_price_as_unavailable() -> None:
    # Given
    product = make_product("1", amount=0, regular_amount=100)

    # When
    entry = DDStoreStrategy().get_entry_data(product)

    # Then
    assert entry.source_metrics == (
        SourceMetric(label="Price", value="Ask for price"),
        SourceMetric(label="Stock", value="In stock"),
    )


def test_ddstore_price_monitor_does_not_snapshot_unavailable_price(
    tmp_path: Path,
) -> None:
    # Given
    sender = RecordingSender([])
    catalog = CatalogStub([(make_product("1", amount=0, regular_amount=0),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()

        # Then
        assert store.load_price_snapshots("ddstore") == ()
        assert sender.messages == []


def test_ddstore_unavailable_product_does_not_consume_snapshot_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(ddstore_price_monitor, "MAX_DDSTORE_RETAINED_SNAPSHOTS", 1)
    sender = RecordingSender([])
    catalog = CatalogStub([(make_product("new", amount=0, regular_amount=0),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        existing = PriceSnapshot("ddstore", "old", Decimal(100), "100 ден.", "MKD")
        store.upsert_price_snapshot(existing)
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()

        # Then
        assert store.load_price_snapshots("ddstore") == (existing,)
        assert sender.messages == []


def test_ddstore_price_monitor_resumes_from_last_real_price(
    tmp_path: Path,
) -> None:
    # Given
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub(
        [
            (make_product("1", amount=100),),
            (make_product("1", amount=0, regular_amount=0),),
            (make_product("1", amount=125),),
        ],
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()
        monitor.scan()

        # Then
        assert len(sender.messages) == 1
        assert sender.messages[0].entry.description == (
            "Price increased from 100 ден. to 125 ден."
        )
        assert store.load_price_snapshots("ddstore")[0].amount == Decimal(125)


def test_ddstore_price_monitor_preserves_last_price_while_unavailable(
    tmp_path: Path,
) -> None:
    # Given
    sender = RecordingSender([])
    catalog = CatalogStub([(make_product("1", amount=0, regular_amount=0),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshot(
            PriceSnapshot("ddstore", "1", Decimal(100), "100 ден.", "MKD"),
        )
        monitor = make_monitor(make_feed(), catalog, store, sender)

        # When
        monitor.scan()

        # Then
        assert store.load_price_snapshots("ddstore") == (
            PriceSnapshot("ddstore", "1", Decimal(100), "100 ден.", "MKD"),
        )
        assert sender.messages == []
