import sqlite3
from collections.abc import Callable, Sequence
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
from rss2discord.transports.setec_models import SetecPriceEntry, SetecProduct
from rss2discord.transports.setec_price_monitor import (
    SetecCatalog,
    SetecPriceMonitor,
    SetecPriceMonitorDependencies,
)


def price_entry_for(product: SetecProduct) -> SetecPriceEntry:
    """Project a product down to what the price index would carry for it."""
    variants = [
        {
            "calculated_price": {
                "calculated_amount": variant.calculated_price.calculated_amount,
                "currency_code": variant.calculated_price.currency_code,
            },
        }
        for variant in product.variants
    ]
    return SetecPriceEntry.model_validate({"id": product.id, "variants": variants})


def make_price_entry(
    product_id: str,
    *,
    calculated_amount: Decimal | int | None = 1_499,
) -> SetecPriceEntry:
    """Build a price-index entry directly, optionally carrying no price at all."""
    variants = (
        []
        if calculated_amount is None
        else [
            {
                "calculated_price": {
                    "calculated_amount": calculated_amount,
                    "currency_code": "mkd",
                },
            },
        ]
    )
    return SetecPriceEntry.model_validate({"id": product_id, "variants": variants})


class CatalogStub:
    """Serve one catalogue per scan as a price index plus display lookups.

    Tests supply whole products; the stub derives the price index from them and
    answers display lookups from the same catalogue, so a scan sees a consistent
    view across both phases unless a test deliberately perturbs one.
    """

    def __init__(
        self,
        batches: list[tuple[SetecProduct, ...]],
        *,
        hidden_ids: frozenset[str] = frozenset(),
        extra_products: tuple[SetecProduct, ...] = (),
        reverse_display: bool = False,
        display_overrides: dict[str, SetecProduct] | None = None,
    ) -> None:
        self._batches = batches
        self._hidden_ids = hidden_ids
        self._extra_products = extra_products
        self._reverse_display = reverse_display
        self._display_overrides = display_overrides or {}
        self._catalog: dict[str, SetecProduct] = {}
        self.urls: list[str] = []
        self.requested_id_batches: list[tuple[str, ...]] = []

    def fetch_price_index(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]:
        del retry_policy, is_shutdown_requested
        self.urls.append(url)
        products = self._batches.pop(0)
        self._catalog = {product.id: product for product in products}
        return tuple(price_entry_for(product) for product in products)

    def fetch_products_by_ids(
        self,
        url: str,
        product_ids: Sequence[str],
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        self.requested_id_batches.append(tuple(product_ids))
        found = [
            self._display_overrides.get(product_id) or self._catalog[product_id]
            for product_id in product_ids
            if product_id in self._catalog and product_id not in self._hidden_ids
        ]
        if self._reverse_display:
            found.reverse()
        return (*found, *self._extra_products)


class RetryingFailureCatalog:
    """Fail every price-index attempt through the caller's retry policy."""

    def __init__(self) -> None:
        self.requested_id_batches: list[tuple[str, ...]] = []

    def fetch_price_index(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]:
        del url, is_shutdown_requested
        return retry_policy.execute(self._fail)

    def fetch_products_by_ids(
        self,
        url: str,
        product_ids: Sequence[str],
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        self.requested_id_batches.append(tuple(product_ids))
        return ()

    @staticmethod
    def _fail() -> tuple[SetecPriceEntry, ...]:
        raise FeedFetchError("Setec", "NetworkError", retryable=True)


class RetryingProductFetchCatalog:
    """Serve a price index once, then fail every display lookup through retries."""

    def __init__(self, entries: tuple[SetecPriceEntry, ...]) -> None:
        self._entries = entries
        self.urls: list[str] = []
        self.requested_id_batches: list[tuple[str, ...]] = []

    def fetch_price_index(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]:
        del retry_policy, is_shutdown_requested
        self.urls.append(url)
        return self._entries

    def fetch_products_by_ids(
        self,
        url: str,
        product_ids: Sequence[str],
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        del url, is_shutdown_requested
        self.requested_id_batches.append(tuple(product_ids))
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
            "product_categories": [{"name": "Computers"}, {"name": "Accessories"}],
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
