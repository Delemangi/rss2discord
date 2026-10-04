"""Sequential tax-inclusive price comparison and Discord delivery for Neksio."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import (
    DiscordSender,
    WebhookMessage,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import SourceMetric
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.neksio import _entry_data
from rss2discord.transports.neksio_catalog_http import NEKSIO_LABEL
from rss2discord.transports.neksio_models import NeksioProduct
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_price_changes,
    finish_price_delivery,
    pause_price_fetch_failure,
    prepare_price_delivery,
    price_direction,
)

MAX_NEKSIO_RETAINED_SNAPSHOTS: Final = 50_000
MAX_NEKSIO_PRICE_CHANGES_PER_SCAN: Final = 100


class NeksioCatalog(Protocol):
    """Retrieve a validated full Neksio catalog in API order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeksioProduct, ...]: ...


@dataclass(frozen=True, slots=True)
class NeksioPriceMonitorDependencies:
    """Typed collaborators used by one Neksio price-monitor scan."""

    catalog: NeksioCatalog
    snapshots: PriceRecoveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: NeksioProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class NeksioPriceMonitor:
    """Compare one full catalog against persisted snapshots and alert on changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: NeksioPriceMonitorDependencies,
    ) -> None:
        self._feed: FeedConfig = feed
        self._dependencies: NeksioPriceMonitorDependencies = dependencies

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
        """Fetch, classify, persist silent updates, then deliver changed prices in order."""
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.fetch_retry_policy.execute(
            lambda: self._dependencies.catalog.fetch_catalog(
                self._feed.url,
                is_shutdown_requested=(
                    self._dependencies.delivery.is_shutdown_requested
                ),
            ),
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        persisted_snapshots = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(self._feed.id),
        )
        snapshots_by_product = {
            snapshot.product_id: snapshot for snapshot in persisted_snapshots
        }
        retained_product_ids = set(snapshots_by_product)
        retained_product_ids.update(str(product.product_id) for product in products)
        if len(retained_product_ids) > MAX_NEKSIO_RETAINED_SNAPSHOTS:
            raise FeedFetchError(NEKSIO_LABEL, "SnapshotLimitExceeded")
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        current_snapshots: dict[str, PriceSnapshot] = {}

        for product in products:
            current = self._snapshot(product)
            current_snapshots[current.product_id] = current
            previous = snapshots_by_product.get(str(product.product_id))
            if previous is None:
                silent_updates.append(current)
                continue
            if (
                previous.amount == current.amount
                and previous.currency == current.currency
            ):
                if previous.formatted != current.formatted:
                    silent_updates.append(current)
                continue
            changes.append(_PriceChange(product, previous, current))

        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        store = self._dependencies.snapshots
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=store,
                feed_id=self._feed.id,
                provider="neksio",
                changes=tuple(
                    PriceChangeRecord(c.current.product_id, c.previous, c.current)
                    for c in changes
                ),
                current=current_snapshots,
                persisted=snapshots_by_product,
                catalog_count=len(products),
            ),
        )
        if plan.blocked:
            return
        if silent_updates and plan.allow_silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(
                    silent_updates,
                ),
            )

        by_id = {change.current.product_id: change for change in changes}
        deliver_price_changes(
            (by_id[product_id] for product_id in plan.selected_ids),
            self._dependencies,
            self._message_for,
            plan=plan,
        )
        finish_price_delivery(store, self._feed.id, plan, len(current_snapshots))

    def _snapshot(self, product: NeksioProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.product_id),
            amount=Decimal(str(product.price_with_tax)),
            formatted=product.formatted_price,
            currency="MKD",
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                _entry_data(product),
                source_metrics=self._metrics_for(change),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or NEKSIO_LABEL,
        )

    @staticmethod
    def _metrics_for(change: _PriceChange) -> tuple[SourceMetric, ...]:
        product = change.product
        metrics = [
            SourceMetric(label="Price", value=change.current.formatted),
            SourceMetric(label="Previous", value=change.previous.formatted, prior=True),
        ]
        if product.old_formatted_price:
            metrics.append(
                SourceMetric(label="Original", value=product.old_formatted_price),
            )
        if product.product_code:
            metrics.append(
                SourceMetric(label="Product code", value=product.product_code),
            )
        if product.manufacturer:
            metrics.append(
                SourceMetric(label="Manufacturer", value=product.manufacturer),
            )
        metrics.append(SourceMetric(label="Stock", value=str(product.stock_quantity)))
        return tuple(metrics)
