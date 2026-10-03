"""Sequential DDStore price comparison and Discord delivery for one feed."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, SourceMetric
from rss2discord.recovery_models import PriceChangeRecord
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
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_price_changes,
    finish_price_delivery,
    pause_price_fetch_failure,
    persist_price_delivery,
    prepare_price_delivery,
    price_direction,
)

MAX_DDSTORE_RETAINED_SNAPSHOTS: Final = 50_000


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
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        persisted = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id,
                limit=MAX_DDSTORE_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_DDSTORE_RETAINED_SNAPSHOTS:
            raise FeedFetchError(DDSTORE_LABEL, "SnapshotLimitExceeded")
        by_id = {snapshot.product_id: snapshot for snapshot in persisted}
        if len({product.uid for product in products}) != len(products):
            raise FeedFetchError(DDSTORE_LABEL, "ConflictingProductIDs")
        available = tuple(
            product
            for product in products
            if is_ddstore_price_available(
                product.price_range.minimum_price.final_price.value,
            )
        )
        if (
            len(set(by_id).union(product.uid for product in available))
            > MAX_DDSTORE_RETAINED_SNAPSHOTS
        ):
            raise FeedFetchError(DDSTORE_LABEL, "SnapshotLimitExceeded")
        current_by_id: dict[str, PriceSnapshot] = {}
        silent: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        for product in available:
            current = self._snapshot(product)
            current_by_id[product.uid] = current
            previous = by_id.get(product.uid)
            if previous is None:
                silent.append(current)
            elif (
                previous.amount != current.amount
                or previous.currency != current.currency
            ):
                changes.append(_PriceChange(product, previous, current))
            elif previous.formatted != current.formatted:
                silent.append(current)
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        store = self._dependencies.snapshots
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=store,
                feed_id=self._feed.id,
                provider=DDSTORE_LABEL,
                changes=tuple(
                    PriceChangeRecord(c.current.product_id, c.previous, c.current)
                    for c in changes
                ),
                current=current_by_id,
                persisted=by_id,
                catalog_count=len(products),
            ),
        )
        if plan.blocked:
            return
        if silent and plan.allow_silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: store.upsert_price_snapshots(silent),
            )
        changes_by_id = {change.current.product_id: change for change in changes}
        deliver_price_changes(
            (changes_by_id[product_id] for product_id in plan.selected_ids),
            self._dependencies,
            self._message_for,
            on_delivered=partial(persist_price_delivery, store, plan),
        )
        finish_price_delivery(store, self._feed.id, plan, len(current_by_id))

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
