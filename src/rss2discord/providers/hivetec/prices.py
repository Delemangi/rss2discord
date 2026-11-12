"""Sequential Hivetec price comparison and Discord delivery for one feed."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData
from rss2discord.providers.hivetec.bounds import HIVETEC_LABEL
from rss2discord.providers.hivetec.models import HivetecProduct
from rss2discord.providers.hivetec.strategy import hivetec_product_metrics
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.catalog_normalization import (
    MAX_HIVETEC_RETAINED_SNAPSHOTS,
    normalize_hivetec_catalog,
)
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceObservation,
    PriceRecoveryStore,
    price_direction,
    run_price_scan,
)


class HivetecCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[HivetecProduct, ...]: ...


class HivetecSnapshotStore(PriceRecoveryStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class HivetecPriceMonitorDependencies:
    catalog: HivetecCatalog
    snapshots: HivetecSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


class HivetecPriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: HivetecPriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        persisted = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id,
                limit=MAX_HIVETEC_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_HIVETEC_RETAINED_SNAPSHOTS:
            raise FeedFetchError(HIVETEC_LABEL, "SnapshotLimitExceeded")
        normalized = normalize_hivetec_catalog(
            self._feed.id,
            products,
            persisted,
            snapshot_limit=MAX_HIVETEC_RETAINED_SNAPSHOTS,
        )
        products_by_id = {str(product.id): product for product in products}
        observations = tuple(
            PriceObservation(products_by_id[item.product_id], item.snapshot)
            for item in normalized
            if item.snapshot is not None
        )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider=HIVETEC_LABEL,
            label=HIVETEC_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_HIVETEC_RETAINED_SNAPSHOTS,
        )

    def _message_for(
        self,
        product: HivetecProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.permalink,
                description="",
                author="",
                timestamp=None,
                image_url=product.image_url,
                categories=tuple(category.name for category in product.categories),
                source_metrics=hivetec_product_metrics(
                    product,
                    previous_price=previous.formatted,
                ),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or HIVETEC_LABEL,
        )
