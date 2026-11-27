from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordSender,
    SleepCallback,
)
from rss2discord.models import PriceSnapshot
from rss2discord.providers.neksio.models import NeksioProduct
from rss2discord.providers.neksio.prices import (
    NeksioCatalog,
    NeksioPriceMonitor,
)
from rss2discord.transports.price_monitor import PriceRecoveryStore
from tests.fakes import (
    RecordingSender,
    RetrySleepAdapter,
    is_not_shutdown,
    keep_running,
    make_price_monitor_dependencies,
)

__all__ = ("RecordingSender", "RetrySleepAdapter")


class CatalogStub:
    def __init__(self, batches: list[tuple[NeksioProduct, ...]]) -> None:
        self._batches = batches
        self.urls: list[str] = []
        self.shutdown_callbacks: list[Callable[[], bool]] = []

    def fetch_catalog(
        self,
        url: str,
        *,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeksioProduct, ...]:
        self.urls.append(url)
        self.shutdown_callbacks.append(is_shutdown_requested)
        return self._batches.pop(0)


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="neksio",
        name="Neksio Deals",
        url="https://g.store.neksio.mk/price-feed",
        webhook="https://discord.example.test/webhooks/id/hidden",
        strategy="neksio",
    )


def make_product(
    product_id: int,
    *,
    amount: str,
    formatted: str,
    old_formatted_price: str | None = "150 MKD",
    category: str = "Laptops",
    subcategory: str = "Gaming",
    manufacturer: str = "Neksio",
    stock_quantity: int = 3,
) -> NeksioProduct:
    return NeksioProduct(
        product_id=product_id,
        product_name=f"Product {product_id}",
        product_code=f"CODE-{product_id}",
        category=category,
        subcategory=subcategory,
        manufacturer=manufacturer,
        price_with_tax=Decimal(amount),
        formatted_price=formatted,
        old_formatted_price=old_formatted_price,
        image_path=f"/images/{product_id}.jpg",
        stock_quantity=stock_quantity,
        observed_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    )


def make_monitor(
    feed: FeedConfig,
    catalog: NeksioCatalog,
    snapshots: PriceRecoveryStore,
    sender: DiscordSender,
    *,
    sleep: SleepCallback = keep_running,
    delay_between_posts: float = 0,
    is_shutdown_requested: Callable[[], bool] = is_not_shutdown,
) -> NeksioPriceMonitor:
    return NeksioPriceMonitor(
        feed,
        make_price_monitor_dependencies(
            catalog,
            snapshots,
            sender,
            sleep=sleep,
            delay_between_posts=delay_between_posts,
            is_shutdown_requested=is_shutdown_requested,
        ),
    )


def snapshots_by_product(
    store: DeliveryStore,
) -> dict[int, PriceSnapshot]:
    return {
        int(snapshot.product_id): snapshot
        for snapshot in store.load_price_snapshots("neksio")
    }
