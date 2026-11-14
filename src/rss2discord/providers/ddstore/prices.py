"""Sequential DDStore price comparison and Discord delivery for one feed."""

from collections.abc import Callable
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, PriceSnapshot, SourceMetric
from rss2discord.providers.ddstore.client import DDSTORE_LABEL
from rss2discord.providers.ddstore.models import DDStoreProduct
from rss2discord.providers.ddstore.strategy import (
    format_ddstore_mkd,
    format_ddstore_stock,
    is_ddstore_price_available,
)
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
)
from rss2discord.transports.catalog_normalization import (
    CatalogObservation,
    validate_catalog_observations,
)
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
    PriceObservation,
    price_direction,
    run_price_scan,
)

MAX_DDSTORE_RETAINED_SNAPSHOTS = 50_000


def ddstore_snapshot(feed_id: str, product: DDStoreProduct) -> PriceSnapshot:
    price = product.price_range.minimum_price.final_price
    return PriceSnapshot(
        feed_id,
        product.uid,
        price.value,
        format_ddstore_mkd(price.value),
        price.currency,
    )


def normalize_ddstore_catalog(
    feed_id: str,
    products: tuple[DDStoreProduct, ...],
    persisted: tuple[PriceSnapshot, ...],
    *,
    snapshot_limit: int = MAX_DDSTORE_RETAINED_SNAPSHOTS,
) -> tuple[CatalogObservation, ...]:
    observations = tuple(
        CatalogObservation(
            product.uid,
            ddstore_snapshot(feed_id, product)
            if is_ddstore_price_available(
                product.price_range.minimum_price.final_price.value,
            )
            else None,
        )
        for product in products
    )
    validate_catalog_observations(
        observations,
        persisted,
        snapshot_limit,
        DDSTORE_LABEL,
    )
    return observations


class DDStoreCatalog(Protocol):
    """Retrieve a validated full DDStore catalog in GraphQL page order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[DDStoreProduct, ...]: ...


class DDStorePriceMonitor:
    """Compare one full DDStore catalog against persisted price snapshots."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[DDStoreCatalog],
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
                limit=MAX_DDSTORE_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_DDSTORE_RETAINED_SNAPSHOTS:
            raise FeedFetchError(DDSTORE_LABEL, "SnapshotLimitExceeded")
        normalized = normalize_ddstore_catalog(
            self._feed.id,
            products,
            persisted,
            snapshot_limit=MAX_DDSTORE_RETAINED_SNAPSHOTS,
        )
        products_by_id = {product.uid: product for product in products}
        observations = tuple(
            PriceObservation(products_by_id[item.product_id], item.snapshot)
            for item in normalized
            if item.snapshot is not None
        )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider=DDSTORE_LABEL,
            label=DDSTORE_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_DDSTORE_RETAINED_SNAPSHOTS,
        )

    def _message_for(
        self,
        product: DDStoreProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
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
                source_metrics=self._metrics_for(product, previous, current),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or DDSTORE_LABEL,
        )

    @staticmethod
    def _metrics_for(
        product: DDStoreProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> tuple[SourceMetric, ...]:
        minimum_price = product.price_range.minimum_price
        metrics = [
            SourceMetric(label="Price", value=current.formatted),
            SourceMetric(label="Previous", value=previous.formatted, prior=True),
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
                value=format_ddstore_stock(product.stock_status),
            ),
        )
        return tuple(metrics)
