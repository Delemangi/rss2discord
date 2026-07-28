"""Sequential DDStore price comparison and Discord delivery for one feed."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult, DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, SourceMetric
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.ddstore import (
    format_ddstore_mkd,
    format_ddstore_stock,
    is_ddstore_price_available,
)
from rss2discord.transports.ddstore_http import DDSTORE_LABEL
from rss2discord.transports.ddstore_models import DDStoreProduct
from rss2discord.transports.price_monitor import PriceAlertDelivery, PriceSnapshotStore

MAX_DDSTORE_RETAINED_SNAPSHOTS: Final = 50_000
MAX_DDSTORE_PRICE_CHANGES_PER_SCAN: Final = 100


class DDStoreCatalog(Protocol):
    """Retrieve a validated full DDStore catalog in GraphQL page order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[DDStoreProduct, ...]: ...


class DDStorePriceSnapshotStore(PriceSnapshotStore, Protocol):
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
        self._feed: FeedConfig = feed
        self._dependencies: DDStorePriceMonitorDependencies = dependencies

    def scan(self) -> None:
        """Fetch, silently snapshot baselines, then deliver changed prices in order."""
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        persisted_snapshots = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id,
                limit=MAX_DDSTORE_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted_snapshots) > MAX_DDSTORE_RETAINED_SNAPSHOTS:
            raise FeedFetchError(DDSTORE_LABEL, "SnapshotLimitExceeded")
        snapshots_by_product = {
            snapshot.product_id: snapshot for snapshot in persisted_snapshots
        }
        available_products = tuple(
            product
            for product in products
            if is_ddstore_price_available(
                product.price_range.minimum_price.final_price.value,
            )
        )
        retained_product_ids = set(snapshots_by_product)
        retained_product_ids.update(product.uid for product in available_products)
        if len(retained_product_ids) > MAX_DDSTORE_RETAINED_SNAPSHOTS:
            raise FeedFetchError(DDSTORE_LABEL, "SnapshotLimitExceeded")
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        for product in available_products:
            current = self._snapshot(product)
            previous = snapshots_by_product.get(product.uid)
            if previous is None:
                silent_updates.append(current)
            elif (
                previous.amount == current.amount
                and previous.currency == current.currency
            ):
                if previous.formatted != current.formatted:
                    silent_updates.append(current)
            else:
                changes.append(_PriceChange(product, previous, current))
        if len(changes) > MAX_DDSTORE_PRICE_CHANGES_PER_SCAN:
            raise FeedFetchError(DDSTORE_LABEL, "PriceChangeLimitExceeded")
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(
                    silent_updates,
                ),
            )
        self._deliver_changes(changes)

    def _deliver_changes(self, changes: list[_PriceChange]) -> None:
        delay_before_next_attempt = False
        for change in changes:
            if self._dependencies.delivery.is_shutdown_requested():
                return
            if (
                delay_before_next_attempt
                and self._dependencies.delivery.delay_between_posts > 0
                and not self._dependencies.delivery.sleep(
                    self._dependencies.delivery.delay_between_posts,
                )
            ):
                return
            delivery_result = self._dependencies.sender.send(
                self._message_for(change),
                self._dependencies.delivery.sleep,
            )
            match delivery_result:
                case DiscordDeliveryResult.DELIVERED:
                    self._persist_changed_snapshot(change.current)
                    delay_before_next_attempt = True
                case DiscordDeliveryResult.FAILED:
                    delay_before_next_attempt = False
                case DiscordDeliveryResult.INTERRUPTED:
                    return
                case unreachable:
                    assert_never(unreachable)

    def _persist_changed_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.upsert_price_snapshot(snapshot),
        )

    def _snapshot(self, product: DDStoreProduct) -> PriceSnapshot:
        final_price = product.price_range.minimum_price.final_price
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.uid,
            amount=final_price.value,
            formatted=format_ddstore_mkd(final_price.value),
            currency=final_price.currency,
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.product_url,
                description=self._description_for(change),
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
            ),
            source_title=self._feed.name or DDSTORE_LABEL,
        )

    @staticmethod
    def _description_for(change: _PriceChange) -> str:
        if change.previous.currency != change.current.currency:
            action = "changed"
        elif change.current.amount < change.previous.amount:
            action = "decreased"
        else:
            action = "increased"
        return f"Price {action} from {change.previous.formatted} to {change.current.formatted}"

    @staticmethod
    def _metrics_for(change: _PriceChange) -> tuple[SourceMetric, ...]:
        minimum_price = change.product.price_range.minimum_price
        metrics = [
            SourceMetric(label="Price", value=change.current.formatted),
            SourceMetric(label="Previous", value=change.previous.formatted),
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
