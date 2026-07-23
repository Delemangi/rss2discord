"""Sequential selling-price comparison and Discord delivery for one Anhoch feed."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    DiscordSender,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.models import EntryData, SourceMetric
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.anhoch_catalog import ANHOCH_LABEL, ANHOCH_PRODUCT_BASE_URL
from rss2discord.transports.anhoch_models import AnhochProduct


class AnhochCatalog(Protocol):
    """Retrieve a validated full Anhoch catalog in API order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnhochProduct, ...]: ...


class PriceSnapshotStore(Protocol):
    """Persist Anhoch selling-price snapshots for one feed."""

    def load_price_snapshots(self, feed_id: str) -> tuple[PriceSnapshot, ...]: ...

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None: ...

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None: ...


@dataclass(frozen=True, slots=True)
class PriceAlertDelivery:
    """Control sequential Discord delivery and observe runtime shutdown state."""

    sleep: SleepCallback
    delay_between_posts: float
    is_shutdown_requested: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class AnhochPriceMonitorDependencies:
    """Typed collaborators used by one price-monitor scan."""

    catalog: AnhochCatalog
    snapshots: PriceSnapshotStore
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

        for product in products:
            current = self._snapshot(product)
            previous = snapshots_by_product.get(product.id)
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
        if silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(
                    silent_updates,
                ),
            )

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
            delay_before_next_attempt = False
            if self._dependencies.delivery.is_shutdown_requested():
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
                    if self._dependencies.delivery.is_shutdown_requested():
                        return
                case DiscordDeliveryResult.INTERRUPTED:
                    return
                case unreachable:
                    assert_never(unreachable)

    def _snapshot(self, product: AnhochProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.id,
            amount=product.selling_price.amount,
            formatted=product.selling_price.formatted,
            currency=product.selling_price.currency,
        )

    def _persist_changed_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.upsert_price_snapshot(snapshot),
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=change.product.name,
                link=f"{ANHOCH_PRODUCT_BASE_URL}{change.product.slug}",
                description=self._description_for(change),
                author="",
                timestamp=None,
                image_url=(
                    change.product.base_image.path
                    if change.product.base_image is not None
                    else None
                ),
                source_metrics=self._metrics_for(change),
            ),
            source_title=self._feed.name or ANHOCH_LABEL,
        )

    @staticmethod
    def _description_for(change: _PriceChange) -> str:
        if change.previous.currency != change.current.currency:
            action = "changed"
        elif change.current.amount < change.previous.amount:
            action = "decreased"
        else:
            action = "increased"
        return (
            f"Price {action} from {change.previous.formatted} "
            f"to {change.current.formatted}"
        )

    @staticmethod
    def _metrics_for(change: _PriceChange) -> tuple[SourceMetric, ...]:
        product = change.product
        metrics = [
            SourceMetric(label="Price", value=change.current.formatted),
            SourceMetric(label="Previous", value=change.previous.formatted),
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
