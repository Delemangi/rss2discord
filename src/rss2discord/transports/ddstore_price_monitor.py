"""Sequential DDStore price comparison and Discord delivery for one feed."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, SourceMetric
from rss2discord.retries import (
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.catalog_normalization import (
    MAX_DDSTORE_RETAINED_SNAPSHOTS,
    normalize_ddstore_catalog,
)
from rss2discord.transports.ddstore import (
    format_ddstore_mkd,
    format_ddstore_stock,
)
from rss2discord.transports.ddstore_http import DDSTORE_LABEL
from rss2discord.transports.ddstore_models import DDStoreProduct
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_catalog_price_changes,
    pause_price_fetch_failure,
    prepare_price_scan,
    price_direction,
)


class DDStoreCatalog(Protocol):
    """Retrieve a validated full DDStore catalog in GraphQL page order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[DDStoreProduct, ...]: ...


class DDStorePriceSnapshotStore(PriceRecoveryStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class DDStorePriceMonitorDependencies:
    """Typed collaborators used by one DDStore price-monitor scan."""

    catalog: DDStoreCatalog
    snapshots: DDStorePriceSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: DDStoreProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class DDStorePriceMonitor:
    """Compare one full DDStore catalog against persisted price snapshots."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: DDStorePriceMonitorDependencies,
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
                    limit=MAX_DDSTORE_RETAINED_SNAPSHOTS + 1,
                ),
            ),
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            snapshot_limit=MAX_DDSTORE_RETAINED_SNAPSHOTS,
            label=DDSTORE_LABEL,
        )
        by_id = {snapshot.product_id: snapshot for snapshot in persisted}
        observations = normalize_ddstore_catalog(
            self._feed.id,
            products,
            persisted,
            snapshot_limit=MAX_DDSTORE_RETAINED_SNAPSHOTS,
        )
        products_by_id = {product.uid: product for product in products}
        deliver_catalog_price_changes(
            self._dependencies,
            self._message_for,
            change_for=lambda previous, current: _PriceChange(
                products_by_id[current.product_id],
                previous,
                current,
            ),
            feed_id=self._feed.id,
            provider=DDSTORE_LABEL,
            current={
                item.product_id: item.snapshot
                for item in observations
                if item.snapshot is not None
            },
            persisted=by_id,
            catalog_count=len(products),
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.product_url,
                description="",
                author="",
                timestamp=product.created_at.isoformat(),
                image_url=(
                    product.small_image.url if product.small_image is not None else None
                ),
                categories=tuple(
                    category.name
                    for category in product.categories or ()
                    if category.name is not None
                ),
                source_metrics=self._metrics_for(change),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or DDSTORE_LABEL,
        )

    @staticmethod
    def _metrics_for(change: _PriceChange) -> tuple[SourceMetric, ...]:
        minimum_price = change.product.price_range.minimum_price
        metrics = [
            SourceMetric(label="Price", value=change.current.formatted),
            SourceMetric(label="Previous", value=change.previous.formatted, prior=True),
        ]
        regular_price = minimum_price.regular_price
        if (
            regular_price is not None
            and regular_price.value != minimum_price.final_price.value
        ):
            metrics.append(
                SourceMetric(
                    label="Original",
                    value=format_ddstore_mkd(regular_price.value),
                ),
            )
        metrics.append(
            SourceMetric(
                label="Stock",
                value=format_ddstore_stock(change.product.stock_status),
            ),
        )
        return tuple(metrics)
