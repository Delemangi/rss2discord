from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceDirection
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from rss2discord.providers.cccenter.prices import (
    CCCenterPriceMonitor,
    CCCenterPriceMonitorDependencies,
)
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[CCCenterProduct, ...]]) -> None:
        self._batches = batches
        self._current: tuple[CCCenterProduct, ...] = ()

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[CCCenterProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        self._current = self._batches.pop(0)
        return self._current

    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        del is_shutdown_requested
        by_id = {product.product_id: product for product in self._current}
        return tuple(by_id[listing.product_id] for listing in listings)


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


class SnapshotStoreSpy(DeliveryStore):
    def __init__(self) -> None:
        super().__init__(Path(":memory:"))
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


def test_compare_prices_preserves_classification_order_and_duplicate_products() -> None:
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
    )
    monitor = CCCenterPriceMonitor(feed, None)  # type: ignore[arg-type]
    products = tuple(
        product("100", f"https://cccenter.mk/product/{index}/") for index in range(5)
    )
    duplicate = replace(products[3], name="Duplicate", current_price=Decimal(120))
    observed = (*products, duplicate)
    current = tuple(monitor._snapshot(item) for item in products)
    persisted = {
        current[1].product_id: current[1],
        current[2].product_id: replace(current[2], formatted="old formatting"),
        current[3].product_id: replace(current[3], amount=Decimal(90)),
        current[4].product_id: replace(current[4], currency="EUR"),
    }

    silent, changes, snapshots = monitor._compare_prices(observed, persisted)

    assert silent == [current[0], current[2]]
    assert [change.product for change in changes] == [
        products[3],
        products[4],
        duplicate,
    ]
    assert [change.previous for change in changes] == [
        persisted[products[3].product_id],
        persisted[products[4].product_id],
        persisted[products[3].product_id],
    ]
    assert snapshots[duplicate.product_id] == monitor._snapshot(duplicate)


@pytest.mark.parametrize("detail_price", ["100", "101", None])
def test_confirmation_requires_matching_scalar_detail_price(
    detail_price: str | None,
) -> None:
    feed = FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="cccenter",
    )
    catalog = CatalogStub([(product(detail_price),)])
    dependencies = CCCenterPriceMonitorDependencies(
        catalog=catalog,
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
    monitor = CCCenterPriceMonitor(feed, dependencies)
    catalog.fetch_catalog(
        feed.url,
        retry_policy=dependencies.fetch_retry_policy,
        is_shutdown_requested=lambda: False,
    )
    listing = product("100")
    current = monitor._snapshot(listing)
    _, changes, _ = monitor._compare_prices(
        (listing,),
        {listing.product_id: replace(current, amount=Decimal(90))},
    )

    confirmed = monitor._confirm_changes(changes)

    if detail_price == "100":
        assert len(confirmed) == 1
        assert confirmed[0].product == product(detail_price)
        assert confirmed[0].current == current
        assert confirmed[0].previous.amount == Decimal(90)
    else:
        assert confirmed == []


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
                (replace(product("54000"), is_in_stock=False),),
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
    monitor = CCCenterPriceMonitor(feed, dependencies)

    with pytest.raises(FeedFetchInterruptedError):
        monitor.scan()

    assert snapshots.load_calls == 0
    assert snapshots.persisted_batches == []
