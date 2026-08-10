"""Two-phase calculated-price comparison and Discord delivery for one Setec feed."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    DiscordSender,
    WebhookMessage,
)
from rss2discord.models import EntryData, SourceMetric
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery, PriceSnapshotStore
from rss2discord.transports.setec import SETEC_PRODUCT_BASE_URL, format_setec_mkd
from rss2discord.transports.setec_catalog_bounds import SETEC_LABEL
from rss2discord.transports.setec_models import SetecPriceEntry, SetecProduct


class SetecCatalog(Protocol):
    """Retrieve Setec prices, then display data for selected products only."""

    def fetch_price_index(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]: ...

    def fetch_products_by_ids(
        self,
        url: str,
        product_ids: Sequence[str],
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]: ...


@dataclass(frozen=True, slots=True)
class SetecPriceMonitorDependencies:
    """Typed collaborators used by one Setec price-monitor scan."""

    catalog: SetecCatalog
    snapshots: PriceSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PendingChange:
    product_id: str
    previous: PriceSnapshot
    current: PriceSnapshot


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: SetecProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class SetecPriceMonitor:
    """Compare one price index against persisted snapshots and alert on changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: SetecPriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        """Compare prices, persist silent updates, then alert on changed products."""
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        price_entries = self._dependencies.catalog.fetch_price_index(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        persisted_snapshots = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(self._feed.id),
        )
        snapshot_store = self._dependencies.snapshots
        snapshots_by_product = {
            snapshot.product_id: snapshot for snapshot in persisted_snapshots
        }
        silent_updates: list[PriceSnapshot] = []
        pending_changes: list[_PendingChange] = []

        for entry in price_entries:
            current = self._snapshot(entry)
            if current is None:
                continue
            previous = snapshots_by_product.get(entry.id)
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
            pending_changes.append(_PendingChange(entry.id, previous, current))

        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: snapshot_store.upsert_price_snapshots(silent_updates),
            )

        if not pending_changes:
            return
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        changes = self._resolve_changes(pending_changes)
        self._deliver(changes)

    def _resolve_changes(
        self,
        pending_changes: Sequence[_PendingChange],
    ) -> list[_PriceChange]:
        products = self._dependencies.catalog.fetch_products_by_ids(
            self._feed.url,
            [pending.product_id for pending in pending_changes],
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        products_by_id = {product.id: product for product in products}
        return [
            _PriceChange(
                products_by_id[pending.product_id],
                pending.previous,
                pending.current,
            )
            for pending in pending_changes
            if pending.product_id in products_by_id
        ]

    def _deliver(self, changes: Sequence[_PriceChange]) -> None:
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

    def _snapshot(self, entry: SetecPriceEntry) -> PriceSnapshot | None:
        calculated_amount = entry.calculated_amount
        if calculated_amount is None:
            return None
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=entry.id,
            amount=calculated_amount,
            formatted=format_setec_mkd(calculated_amount),
            currency="MKD",
        )

    def _persist_changed_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.upsert_price_snapshot(snapshot),
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.title,
                link=f"{SETEC_PRODUCT_BASE_URL}{product.handle}",
                description=self._description_for(change),
                author="",
                timestamp=None,
                image_url=product.thumbnail,
                categories=tuple(category.name for category in product.categories),
                source_metrics=self._metrics_for(change),
            ),
            source_title=self._feed.name or SETEC_LABEL,
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
        metrics = [
            SourceMetric(label="Price", value=change.current.formatted),
            SourceMetric(label="Previous", value=change.previous.formatted),
        ]
        if not change.product.variants:
            return tuple(metrics)
        calculated_price = change.product.variants[0].calculated_price
        if calculated_price.calculated_amount != change.current.amount:
            # The display fetch saw a different price than the one being reported,
            # so its original amount does not belong beside this alert's price.
            return tuple(metrics)
        if calculated_price.original_amount != calculated_price.calculated_amount:
            metrics.append(
                SourceMetric(
                    label="Original",
                    value=format_setec_mkd(calculated_price.original_amount),
                ),
            )
        return tuple(metrics)
