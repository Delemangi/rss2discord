from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceDirection, SourceMetric
from rss2discord.providers.ananas.bounds import ANANAS_FEED_URL
from rss2discord.providers.ananas.models import AnanasProduct
from rss2discord.providers.ananas.prices import (
    AnanasCatalog,
    AnanasPriceMonitor,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)
from tests.setec_price_monitor_helpers import RecordingSender

AnanasPriceMonitorDependencies = PriceMonitorDependencies[AnanasCatalog]


class CatalogStub:
    def __init__(self, batches: list[tuple[AnanasProduct, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnanasProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def make_product(
    *,
    product_id: str = "alpha",
    inventory_id: str = "1",
    price: Decimal | int = 1000,
    base_price: Decimal | int | None = 1200,
    in_stock: bool = True,
    merchant: str = "Ananas Shop",
) -> AnanasProduct:
    return AnanasProduct(
        inventory_id=inventory_id,
        product_id=product_id,
        name=f"Product {product_id}",
        url=f"https://ananas.mk/proizvod/{product_id}/{inventory_id}",
        image_url="https://ananas.mk/tmp/image-thumbnails/item.jpg",
        brand="Brand",
        categories=("IT Shop", "Computers"),
        price=Decimal(price),
        base_price=None if base_price is None else Decimal(base_price),
        currency="MKD",
        in_stock=in_stock,
        available=2,
        merchant=merchant,
        first_published_ms=1_710_511_159_942,
        has_variations=False,
    )


def make_monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> AnanasPriceMonitor:
    return AnanasPriceMonitor(
        FeedConfig(
            id="ananas",
            name="Ananas Computers",
            url=ANANAS_FEED_URL,
            webhook="https://discord.test/webhooks/id/token",
            strategy="ananas",
        ),
        AnanasPriceMonitorDependencies(
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


def test_price_monitor_creates_slug_snapshot_with_source_format(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub([(make_product(price=7403, base_price=None),)]),
            store,
            sender,
        )

        monitor.scan()

        snapshots = store.load_price_snapshots("ananas")
        assert len(snapshots) == 1
        assert snapshots[0].product_id == "alpha"
        assert snapshots[0].amount == Decimal(7403)
        assert snapshots[0].formatted == "7.403 ДЕН"
        assert snapshots[0].currency == "MKD"
        assert sender.messages == []


def test_price_monitor_delivers_price_change_message(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    (make_product(price=1000, base_price=1200),),
                    (make_product(price=900, base_price=1200),),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.title == "Product alpha"
        assert message.entry.link == "https://ananas.mk/proizvod/alpha/1"
        assert message.entry.image_url == (
            "https://ananas.mk/tmp/image-thumbnails/item.jpg"
        )
        assert message.entry.categories == ("IT Shop", "Computers")
        assert message.entry.timestamp == "2024-03-15T13:59:19.942000+00:00"
        assert message.entry.price_direction is PriceDirection.DECREASE
        assert message.entry.source_metrics == (
            SourceMetric("Price", "900 ДЕН"),
            SourceMetric("Previous", "1.000 ДЕН", prior=True),
            SourceMetric("Original", "1.200 ДЕН"),
            SourceMetric("Stock", "In stock"),
            SourceMetric("Merchant", "Ananas Shop"),
        )
        assert store.load_price_snapshots("ananas")[0].amount == Decimal(900)


def test_price_monitor_excludes_out_of_stock_products(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    (make_product(price=1000, in_stock=False),),
                    (make_product(price=1000, in_stock=True),),
                    (make_product(price=900, in_stock=False),),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        assert store.load_price_snapshots("ananas") == ()

        monitor.scan()
        assert store.load_price_snapshots("ananas")[0].amount == Decimal(1000)

        monitor.scan()
        assert sender.messages == []
        assert store.load_price_snapshots("ananas")[0].amount == Decimal(1000)


def test_price_monitor_groups_observations_by_slug(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    (
                        make_product(inventory_id="1", price=1000),
                        make_product(inventory_id="2", price=900),
                    ),
                    (
                        make_product(inventory_id="1", price=800),
                        make_product(inventory_id="2", price=700),
                    ),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        monitor.scan()

        snapshots = store.load_price_snapshots("ananas")
        assert len(snapshots) == 1
        assert snapshots[0].product_id == "alpha"
        assert snapshots[0].amount == Decimal(700)
        assert len(sender.messages) == 1
