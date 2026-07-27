import sqlite3
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import SourceMetric
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.ddstore_models import DDStoreProduct
from rss2discord.transports.ddstore_price_monitor import (
    DDStorePriceMonitor,
    DDStorePriceMonitorDependencies,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[DDStoreProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[DDStoreProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def make_product(
    product_id: str,
    *,
    amount: Decimal | int = 1_499,
    regular_amount: Decimal | int | None = None,
    stock_status: str = "IN_STOCK",
    created_at: str = "2024-07-09 08:54:25",
) -> DDStoreProduct:
    return DDStoreProduct.model_validate(
        {
            "uid": product_id,
            "sku": f"SKU-{product_id}",
            "name": f"Product {product_id}",
            "url_key": f"products/product-{product_id}",
            "url_suffix": ".html",
            "created_at": created_at,
            "stock_status": stock_status,
            "small_image": {"url": f"https://ddstore.mk/media/{product_id}.webp"},
            "categories": [{"uid": "1", "name": "Computers", "url_path": "computers"}],
            "price_range": {
                "minimum_price": {
                    "final_price": {"value": amount, "currency": "MKD"},
                    "regular_price": {
                        "value": amount if regular_amount is None else regular_amount,
                        "currency": "MKD",
                    },
                },
            },
        },
    )


def make_monitor(
    feed: FeedConfig,
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> DDStorePriceMonitor:
    return DDStorePriceMonitor(
        feed,
        DDStorePriceMonitorDependencies(
            catalog=catalog,
            snapshots=store,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        ),
    )


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="ddstore",
        name="DDStore Deals",
        url="https://ddstore.mk/catalog",
        webhook="https://discord.example.test/webhooks/id/hidden",
        strategy="ddstore",
    )


def test_ddstore_price_monitor_silently_seeds_then_delivers_price_change(
    tmp_path: Path,
) -> None:
    # Given
    baseline = make_product("1", amount=100, regular_amount=120)
    changed = make_product(
        "1",
        amount=Decimal("90.50"),
        regular_amount=120,
        stock_status="OUT_OF_STOCK",
        created_at="2024-07-10 09:10:11",
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    feed = make_feed()
    catalog = CatalogStub([(baseline,), (changed,)])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(feed, catalog, store, sender)

        # When
        monitor.scan()
        monitor.scan()

        # Then
        assert len(sender.messages) == 1
        assert (
            sender.messages[0].entry.description
            == "Price decreased from 100 ден. to 90,5 ден."
        )
        assert sender.messages[0].entry.source_metrics == (
            SourceMetric(label="Price", value="90,5 ден."),
            SourceMetric(label="Previous", value="100 ден."),
            SourceMetric(label="Original", value="120 ден."),
            SourceMetric(label="Stock", value="Out of stock"),
        )
        assert sender.messages[0].entry.timestamp == "2024-07-10T09:10:11+00:00"
        snapshots = {
            snapshot.product_id: snapshot
            for snapshot in store.load_price_snapshots("ddstore")
        }
        assert snapshots["1"].amount == Decimal("90.50")


def test_ddstore_price_monitor_retains_history_while_seeding_current_products(
    tmp_path: Path,
) -> None:
    # Given
    catalog = CatalogStub([(make_product("new"),)])
    sender = RecordingSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.upsert_price_snapshots(
            [
                PriceSnapshot("ddstore", "old-1", Decimal(1), "1 ден.", "MKD"),
                PriceSnapshot("ddstore", "old-2", Decimal(1), "1 ден.", "MKD"),
            ],
        )

        # When
        make_monitor(make_feed(), catalog, store, sender).scan()

        # Then
        assert {
            snapshot.product_id for snapshot in store.load_price_snapshots("ddstore")
        } == {"old-1", "old-2", "new"}


def test_ddstore_price_monitor_delivers_more_than_one_hundred_changes(
    tmp_path: Path,
) -> None:
    # Given
    baseline = tuple(make_product(str(index), amount=100) for index in range(101))
    changed = tuple(make_product(str(index), amount=90) for index in range(101))
    catalog = CatalogStub(
        [baseline, changed],
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED] * 101)

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)
        monitor.scan()

        # When
        monitor.scan()

        # Then
        assert len(sender.messages) == 101


@pytest.mark.parametrize(
    "outcome",
    [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.INTERRUPTED],
)
def test_ddstore_price_monitor_does_not_advance_failed_or_interrupted_delivery(
    tmp_path: Path,
    outcome: DiscordDeliveryResult,
) -> None:
    # Given
    catalog = CatalogStub(
        [(make_product("1", amount=100),), (make_product("1", amount=90),)],
    )
    sender = RecordingSender([outcome])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)
        monitor.scan()

        # When
        monitor.scan()

        # Then
        snapshot = store.load_price_snapshots("ddstore")[0]
        assert snapshot.amount == Decimal(100)


def test_ddstore_price_monitor_surfaces_persistence_failure_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    catalog = CatalogStub(
        [(make_product("1", amount=100),), (make_product("1", amount=90),)],
    )
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(make_feed(), catalog, store, sender)
        monitor.scan()

        def fail_persistence(snapshot: PriceSnapshot) -> None:
            del snapshot
            raise sqlite3.OperationalError("disk failure")

        monkeypatch.setattr(store, "upsert_price_snapshot", fail_persistence)

        # When / Then
        with pytest.raises(sqlite3.OperationalError, match="disk failure"):
            monitor.scan()
        assert len(sender.messages) == 1
        assert store.load_price_snapshots("ddstore")[0].amount == Decimal(100)
