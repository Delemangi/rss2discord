from collections.abc import Callable, Iterable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceDirection
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.cccenter_models import CCCenterProduct
from rss2discord.transports.cccenter_price_monitor import (
    CCCenterPriceMonitor,
    CCCenterPriceMonitorDependencies,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[CCCenterProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[CCCenterProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


class ShutdownAfterFetchCatalog(CatalogStub):
    def __init__(self, batches: list[tuple[CCCenterProduct, ...]]) -> None:
        super().__init__(batches)
        self.shutdown = False

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[CCCenterProduct, ...]:
        products = super().fetch_catalog(
            url,
            retry_policy=retry_policy,
            is_shutdown_requested=is_shutdown_requested,
        )
        self.shutdown = True
        return products


class SnapshotStoreSpy:
    def __init__(self) -> None:
        self.load_calls = 0
        self.persisted_batches: list[tuple[PriceSnapshot, ...]] = []

    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]:
        del feed_id, limit
        self.load_calls += 1
        return ()

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        self.persisted_batches.append((snapshot,))

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None:
        self.persisted_batches.append(tuple(snapshots))


def product(
    price: str | None,
    product_id: str = "https://cccenter.mk/product/a/",
) -> CCCenterProduct:
    return CCCenterProduct(
        product_id=product_id,
        name="Alpha",
        url=product_id,
        sku="A-1",
        current_price=None if price is None else Decimal(price),
        original_price=None,
        image_url=None,
        categories=("Лаптопи",),
        is_in_stock=True,
    )


def test_cccenter_price_monitor_baselines_silently_then_alerts_changes_and_skips_unpriced(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
    )
    dependencies = CCCenterPriceMonitorDependencies(
        catalog=CatalogStub(
            [
                (product("56000"), product(None, "https://cccenter.mk/product/b/")),
                (product("54000"),),
            ],
        ),
        snapshots=None,  # type: ignore[arg-type]
        sender=sender,
        fetch_retry_policy=FetchRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=lambda _: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        ),
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = CCCenterPriceMonitor(feed, replace(dependencies, snapshots=store))
        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        assert sender.messages[0].entry.price_direction is PriceDirection.DECREASE
        assert str(store.load_price_snapshots("cccenter")[0].amount) == "54000"


def test_cccenter_price_monitor_skips_variable_products(
    tmp_path: Path,
) -> None:
    variable = replace(product("56000"), price_status="variable")
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
    )
    dependencies = CCCenterPriceMonitorDependencies(
        catalog=CatalogStub([(variable,)]),
        snapshots=None,  # type: ignore[arg-type]
        sender=RecordingSender([]),
        fetch_retry_policy=FetchRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=lambda _: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        ),
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        CCCenterPriceMonitor(feed, replace(dependencies, snapshots=store)).scan()
        assert store.load_price_snapshots("cccenter") == ()


def test_cccenter_shutdown_after_fetch_skips_snapshot_reads_and_writes() -> None:
    catalog = ShutdownAfterFetchCatalog([(product("56000"),)])
    snapshots = SnapshotStoreSpy()
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
    )
    dependencies = CCCenterPriceMonitorDependencies(
        catalog=catalog,
        snapshots=snapshots,
        sender=RecordingSender([]),
        fetch_retry_policy=FetchRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=lambda _: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: catalog.shutdown,
        ),
    )

    with pytest.raises(FeedFetchInterruptedError):
        CCCenterPriceMonitor(feed, dependencies).scan()

    assert snapshots.load_calls == 0
    assert snapshots.persisted_batches == []
