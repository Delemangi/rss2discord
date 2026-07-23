import sqlite3
from collections.abc import Callable
from decimal import Decimal

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    DiscordSender,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.anhoch_models import (
    AnhochDisplayPrice,
    AnhochImage,
    AnhochInstallments,
    AnhochMoney,
    AnhochProduct,
)
from rss2discord.transports.anhoch_price_monitor import (
    AnhochCatalog,
    AnhochPriceMonitor,
    AnhochPriceMonitorDependencies,
    PriceAlertDelivery,
    PriceSnapshotStore,
)


class CatalogStub:
    def __init__(self, batches: list[tuple[AnhochProduct, ...]]) -> None:
        self._batches: list[tuple[AnhochProduct, ...]] = batches
        self.urls: list[str] = []

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnhochProduct, ...]:
        del retry_policy, is_shutdown_requested
        self.urls.append(url)
        return self._batches.pop(0)


class RetryingFailureCatalog:
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnhochProduct, ...]:
        del url, is_shutdown_requested
        return retry_policy.execute(self._fail)

    @staticmethod
    def _fail() -> tuple[AnhochProduct, ...]:
        raise FeedFetchError("Anhoch", "NetworkError", retryable=True)


class RecordingSender:
    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes: list[bool] = outcomes
        self.messages: list[WebhookMessage] = []

    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        del sleep
        self.messages.append(message)
        return (
            DiscordDeliveryResult.DELIVERED
            if self._outcomes.pop(0)
            else DiscordDeliveryResult.FAILED
        )


class RetrySleepAdapter:
    def __init__(self, sleep: SleepCallback) -> None:
        self._sleep: SleepCallback = sleep

    def __call__(self, seconds: float) -> bool:
        return self._sleep(seconds)


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="anhoch",
        name="Anhoch Deals",
        url="https://catalog.example.test/products?feed_secret=hidden",
        webhook="https://discord.example.test/webhooks/id/hidden",
        strategy="anhoch",
    )


def make_product(
    product_id: int,
    *,
    amount: str,
    formatted: str,
    currency: str = "MKD",
) -> AnhochProduct:
    return AnhochProduct(
        id=product_id,
        name=f"Product {product_id}",
        slug=f"product-{product_id}",
        price=AnhochDisplayPrice(formatted="150 den"),
        selling_price=AnhochMoney(
            amount=Decimal(amount),
            currency=currency,
            formatted=formatted,
        ),
        base_image=AnhochImage(path=f"https://images.example.test/{product_id}.jpg"),
        is_in_stock=True,
        qty=3,
        installments=AnhochInstallments(
            period=12,
            price=AnhochDisplayPrice(formatted="10 den"),
        ),
    )


def keep_running(_seconds: float) -> bool:
    return True


def is_not_shutdown() -> bool:
    return False


def make_monitor(
    feed: FeedConfig,
    catalog: AnhochCatalog,
    snapshots: PriceSnapshotStore,
    sender: DiscordSender,
    *,
    sleep: SleepCallback = keep_running,
    delay_between_posts: float = 0,
    is_shutdown_requested: Callable[[], bool] = is_not_shutdown,
) -> AnhochPriceMonitor:
    retry_sleep = RetrySleepAdapter(sleep)
    return AnhochPriceMonitor(
        feed,
        AnhochPriceMonitorDependencies(
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


def snapshots_by_product(store: DeliveryStore) -> dict[int, PriceSnapshot]:
    return {
        snapshot.product_id: snapshot
        for snapshot in store.load_price_snapshots("anhoch")
    }


def busy_database_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error
