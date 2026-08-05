from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import SourceMetric
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.gjirafa50_models import Gjirafa50Product
from rss2discord.transports.gjirafa50_price_monitor import (
    Gjirafa50PriceMonitor,
    Gjirafa50PriceMonitorDependencies,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.setec_price_monitor_helpers import RecordingSender


class CatalogStub:
    def __init__(self, batches: list[tuple[Gjirafa50Product, ...]]) -> None:
        self.batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Gjirafa50Product, ...]:
        del url, retry_policy, is_shutdown_requested
        return self.batches.pop(0)


def make_product(product_id: int, price: Decimal | int) -> Gjirafa50Product:
    amount = Decimal(price)
    return Gjirafa50Product(
        product_id,
        f"Product {product_id}",
        f"https://gjirafa50.mk/product-{product_id}",
        None,
        amount,
        f"{amount} MKD.",
        datetime(2026, 8, 5, tzinfo=UTC),
    )


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="gjirafa50",
        name="Gjirafa50",
        url="https://gjirafa50.mk/",
        webhook="https://discord.test/webhooks/id/token",
        strategy="gjirafa50",
    )


def make_monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> Gjirafa50PriceMonitor:
    return Gjirafa50PriceMonitor(
        make_feed(),
        Gjirafa50PriceMonitorDependencies(
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
            database_path=store.database_path,
        ),
    )


def test_price_monitor_silently_baselines_then_delivers_price_change(
    tmp_path: Path,
) -> None:
    # Given
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    catalog = CatalogStub([(make_product(1, 100),), (make_product(1, 90),)])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(catalog, store, sender)
        monitor.scan()
        monitor.scan()
        snapshots = store.load_price_snapshots("gjirafa50")

    # Then
    assert len(sender.messages) == 1
    assert sender.messages[0].entry.description == (
        "Price decreased from 100 MKD. to 90 MKD."
    )
    assert sender.messages[0].entry.source_metrics == (
        SourceMetric("Price", "90 MKD."),
        SourceMetric("Previous", "100 MKD."),
    )
    assert snapshots[0].amount == Decimal(90)


def test_price_monitor_does_not_advance_snapshot_after_failed_delivery(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.FAILED])
    catalog = CatalogStub([(make_product(1, 100),), (make_product(1, 90),)])

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(catalog, store, sender)
        monitor.scan()
        monitor.scan()
        snapshot = store.load_price_snapshots("gjirafa50")[0]

    assert snapshot.amount == Decimal(100)
