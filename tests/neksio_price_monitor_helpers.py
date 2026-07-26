from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    DiscordSender,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.anhoch_price_monitor import PriceAlertDelivery
from rss2discord.transports.neksio_models import NeksioProduct
from rss2discord.transports.neksio_price_monitor import (
    NeksioCatalog,
    NeksioPriceMonitor,
    NeksioPriceMonitorDependencies,
    PriceSnapshotStore,
)


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


class RecordingSender:
    def __init__(
        self,
        outcomes: list[bool | DiscordDeliveryResult],
    ) -> None:
        self._outcomes = outcomes
        self.messages: list[WebhookMessage] = []

    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        del sleep
        self.messages.append(message)
        outcome = self._outcomes.pop(0)
        return (
            DiscordDeliveryResult.DELIVERED
            if isinstance(outcome, bool) and outcome
            else (
                DiscordDeliveryResult.FAILED if isinstance(outcome, bool) else outcome
            )
        )


class RetrySleepAdapter:
    def __init__(self, sleep: SleepCallback) -> None:
        self._sleep = sleep

    def __call__(self, seconds: float) -> bool:
        return self._sleep(seconds)


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


def keep_running(_seconds: float) -> bool:
    return True


def shutdown_never_requested() -> bool:
    return False


def make_monitor(
    feed: FeedConfig,
    catalog: NeksioCatalog,
    snapshots: PriceSnapshotStore,
    sender: DiscordSender,
    *,
    sleep: SleepCallback = keep_running,
    delay_between_posts: float = 0,
    is_shutdown_requested: Callable[[], bool] = shutdown_never_requested,
) -> NeksioPriceMonitor:
    retry_sleep = RetrySleepAdapter(sleep)
    return NeksioPriceMonitor(
        feed,
        NeksioPriceMonitorDependencies(
            catalog=catalog,
            snapshots=snapshots,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=retry_sleep,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=retry_sleep,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=sleep,
                delay_between_posts=delay_between_posts,
                is_shutdown_requested=is_shutdown_requested,
            ),
        ),
    )


def snapshots_by_product(
    store: DeliveryStore,
) -> dict[int, PriceSnapshot]:
    return {
        int(snapshot.product_id): snapshot
        for snapshot in store.load_price_snapshots("neksio")
    }
