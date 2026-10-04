"""Sequential selling-price comparison and Discord delivery for one Anhoch feed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import (
    DiscordSender,
    WebhookMessage,
)
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, SourceMetric
from rss2discord.providers.anhoch.catalog import ANHOCH_LABEL, ANHOCH_PRODUCT_BASE_URL
from rss2discord.providers.anhoch.models import AnhochProduct
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_price_changes,
    finish_price_delivery,
    pause_price_fetch_failure,
    prepare_price_delivery,
    price_direction,
)


class AnhochCatalog(Protocol):
    """Retrieve a validated full Anhoch catalog in API order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnhochProduct, ...]: ...


@dataclass(frozen=True, slots=True)
class AnhochPriceMonitorDependencies:
    """Typed collaborators used by one price-monitor scan."""

    catalog: AnhochCatalog
    snapshots: PriceRecoveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: AnhochProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class AnhochPriceMonitor:
    """Compare one full catalog against persisted snapshots and alert on changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: AnhochPriceMonitorDependencies,
    ) -> None:
        self._feed: FeedConfig = feed
        self._dependencies: AnhochPriceMonitorDependencies = dependencies

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
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        persisted_snapshots = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(self._feed.id),
        )
        snapshots_by_product = {
            snapshot.product_id: snapshot for snapshot in persisted_snapshots
        }
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        current_snapshots: dict[str, PriceSnapshot] = {}

        for product in products:
            current = self._snapshot(product)
            current_snapshots[current.product_id] = current
            previous = snapshots_by_product.get(str(product.id))
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
                provider="anhoch",
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

    def _snapshot(self, product: AnhochProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.id),
            amount=product.selling_price.amount,
            formatted=product.selling_price.formatted,
            currency=product.selling_price.currency,
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=change.product.name,
                link=f"{ANHOCH_PRODUCT_BASE_URL}{change.product.slug}",
                description="",
                author="",
                timestamp=None,
                image_url=(
                    change.product.base_image.path
                    if change.product.base_image is not None
                    else None
                ),
                source_metrics=self._metrics_for(change),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or ANHOCH_LABEL,
        )

    @staticmethod
    def _metrics_for(change: _PriceChange) -> tuple[SourceMetric, ...]:
        product = change.product
        metrics = [
            SourceMetric(label="Price", value=change.current.formatted),
            SourceMetric(label="Previous", value=change.previous.formatted, prior=True),
        ]
        if product.price.formatted != product.selling_price.formatted:
            metrics.append(
                SourceMetric(label="Original", value=product.price.formatted),
            )
        stock = (
            str(product.qty) if product.is_in_stock and product.qty is not None else "0"
        )
        metrics.append(SourceMetric(label="Stock", value=stock))
        if product.installments is not None:
            metrics.append(
                SourceMetric(
                    label="Installments",
                    value=(
                        f"{product.installments.period} × "
                        f"{product.installments.price.formatted}"
                    ),
                ),
            )
        return tuple(metrics)
