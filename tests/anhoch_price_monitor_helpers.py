from collections.abc import Callable
from decimal import Decimal

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordSender,
    SleepCallback,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import PriceSnapshot
from rss2discord.providers.anhoch.models import (
    AnhochDisplayPrice,
    AnhochImage,
    AnhochInstallments,
    AnhochMoney,
    AnhochProduct,
)
from rss2discord.providers.anhoch.prices import (
    AnhochCatalog,
    AnhochPriceMonitor,
)
from rss2discord.retries import FetchRetryPolicy
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


def make_monitor(
    feed: FeedConfig,
    catalog: AnhochCatalog,
    snapshots: PriceRecoveryStore,
    sender: DiscordSender,
    *,
    sleep: SleepCallback = keep_running,
    delay_between_posts: float = 0,
    is_shutdown_requested: Callable[[], bool] = is_not_shutdown,
) -> AnhochPriceMonitor:
    return AnhochPriceMonitor(
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


def snapshots_by_product(store: DeliveryStore) -> dict[str, PriceSnapshot]:
    return {
        snapshot.product_id: snapshot
        for snapshot in store.load_price_snapshots("anhoch")
    }
