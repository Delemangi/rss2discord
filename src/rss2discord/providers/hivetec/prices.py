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
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.catalog_normalization import (
    MAX_HIVETEC_RETAINED_SNAPSHOTS,
    normalize_hivetec_catalog,
)
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_catalog_price_changes,
    pause_price_fetch_failure,
    prepare_price_scan,
    price_direction,
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


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: HivetecProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class HivetecPriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: HivetecPriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        try:
            self._scan()
        except FeedFetchError as error:
            if not pause_price_fetch_failure(
                self._dependencies.snapshots,
                self._feed.id,
                error,
            ):
                raise

    def _scan(self) -> None:
        products, persisted = prepare_price_scan(
            fetch_products=lambda: self._dependencies.catalog.fetch_catalog(
                self._feed.url,
                retry_policy=self._dependencies.fetch_retry_policy,
                is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            ),
            load_snapshots=lambda: self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.load_price_snapshots(
                    self._feed.id,
                    limit=MAX_HIVETEC_RETAINED_SNAPSHOTS + 1,
                ),
            ),
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            snapshot_limit=MAX_HIVETEC_RETAINED_SNAPSHOTS,
            label=HIVETEC_LABEL,
        )
        by_id = {snapshot.product_id: snapshot for snapshot in persisted}
        observations = normalize_hivetec_catalog(
            self._feed.id,
            products,
            persisted,
            snapshot_limit=MAX_HIVETEC_RETAINED_SNAPSHOTS,
        )
        products_by_id = {str(product.id): product for product in products}
        deliver_catalog_price_changes(
            self._dependencies,
            self._message_for,
            change_for=lambda previous, current: _PriceChange(
                products_by_id[current.product_id],
                previous,
                current,
            ),
            feed_id=self._feed.id,
            provider=HIVETEC_LABEL,
            current={
                item.product_id: item.snapshot
                for item in observations
                if item.snapshot is not None
            },
            persisted=by_id,
            catalog_count=len(products),
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=change.product.name,
                link=change.product.permalink,
                description="",
                author="",
                timestamp=None,
                image_url=change.product.image_url,
                categories=tuple(
                    category.name for category in change.product.categories
                ),
                source_metrics=hivetec_product_metrics(
                    change.product,
                    previous_price=change.previous.formatted,
                ),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or HIVETEC_LABEL,
        )
