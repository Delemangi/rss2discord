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
from rss2discord.transports.price_monitor import PriceAlertDelivery, PriceSnapshotStore
from rss2discord.transports.setec_models import SetecProduct
from rss2discord.transports.setec_price_monitor import (
    SetecCatalog,
    SetecPriceMonitor,
    SetecPriceMonitorDependencies,
)


class CatalogStub:
    def __init__(self, batches: list[tuple[SetecProduct, ...]]) -> None:
        self._batches = batches
        self.urls: list[str] = []

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
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
    ) -> tuple[SetecProduct, ...]:
        del url, is_shutdown_requested
        return retry_policy.execute(self._fail)

    @staticmethod
    def _fail() -> tuple[SetecProduct, ...]:
        raise FeedFetchError("Setec", "NetworkError", retryable=True)


class RecordingSender:
    def __init__(self, outcomes: list[DiscordDeliveryResult]) -> None:
        self._outcomes = outcomes
        self.messages: list[WebhookMessage] = []

    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        del sleep
        self.messages.append(message)
        return self._outcomes.pop(0)


class RetrySleepAdapter:
    def __init__(self, sleep: SleepCallback) -> None:
        self._sleep = sleep

    def __call__(self, seconds: float) -> bool:
        return self._sleep(seconds)


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="setec",
        name="Setec Deals",
        url="https://catalog.example.test/products?feed_secret=hidden",
        webhook="https://discord.example.test/webhooks/id/hidden",
        strategy="setec",
    )


def make_product(
    product_id: str,
    *,
    calculated_amount: Decimal | int | None = 1_499,
    original_amount: Decimal | int | None = None,
) -> SetecProduct:
    variants = (
        []
        if calculated_amount is None
        else [
            {
                "calculated_price": {
                    "calculated_amount": calculated_amount,
                    "original_amount": (
                        calculated_amount
                        if original_amount is None
                        else original_amount
                    ),
                    "currency_code": "mkd",
                },
            },
        ]
    )
    return SetecProduct.model_validate(
        {
            "id": product_id,
            "title": f"Product {product_id}",
            "handle": f"product-{product_id}",
            "thumbnail": f"https://images.example.test/{product_id}.webp",
            "created_at": "2026-07-23T02:24:28.424Z",
            "variants": variants,
            "categories": [{"name": "Computers"}, {"name": "Accessories"}],
        },
    )


def keep_running(_seconds: float) -> bool:
    return True


def is_not_shutdown() -> bool:
    return False


def make_monitor(
    feed: FeedConfig,
    catalog: SetecCatalog,
    snapshots: PriceSnapshotStore,
    sender: DiscordSender,
    *,
    sleep: SleepCallback = keep_running,
    delay_between_posts: float = 0,
    is_shutdown_requested: Callable[[], bool] = is_not_shutdown,
) -> SetecPriceMonitor:
    retry_sleep = RetrySleepAdapter(sleep)
    return SetecPriceMonitor(
        feed,
        SetecPriceMonitorDependencies(
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


def snapshots_by_product(store: DeliveryStore) -> dict[str, PriceSnapshot]:
    return {
        snapshot.product_id: snapshot
        for snapshot in store.load_price_snapshots("setec")
    }


def busy_database_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error
