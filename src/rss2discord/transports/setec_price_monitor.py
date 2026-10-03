"""Two-phase calculated-price comparison and Discord delivery for one Setec feed."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
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
    pause_price_recovery,
    prepare_price_delivery,
    price_direction,
    record_price_health,
)
from rss2discord.transports.setec import SETEC_PRODUCT_BASE_URL, format_setec_mkd
from rss2discord.transports.setec_catalog_bounds import SETEC_LABEL
from rss2discord.transports.setec_models import SetecPriceEntry, SetecProduct

logger = logging.getLogger(__name__)


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
    snapshots: PriceRecoveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PendingChange:
    product_id: str
    previous: PriceSnapshot
    current: PriceSnapshot
    variant_id: str | None


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
        current_snapshots: dict[str, PriceSnapshot] = {}

        for entry in price_entries:
            current = self._snapshot(entry)
            if current is None:
                continue
            current_snapshots[entry.id] = current
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
            pending_changes.append(
                _PendingChange(entry.id, previous, current, entry.variants[0].id),
            )

        ambiguous_count = sum(len(entry.variants) > 1 for entry in price_entries)
        if ambiguous_count:
            logger.warning(
                "Deferred %d ambiguous multi-variant prices for feed %s",
                ambiguous_count,
                self._feed.id,
            )

        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=snapshot_store,
                feed_id=self._feed.id,
                provider="setec",
                changes=tuple(
                    PriceChangeRecord(p.product_id, p.previous, p.current)
                    for p in pending_changes
                ),
                current=current_snapshots,
                persisted=snapshots_by_product,
                catalog_count=len(price_entries),
            ),
        )
        if plan.blocked:
            return
        if silent_updates and plan.allow_silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: snapshot_store.upsert_price_snapshots(silent_updates),
            )

        if not plan.selected_ids:
            if ambiguous_count:
                record_price_health(
                    snapshot_store,
                    self._feed.id,
                    "failed",
                    "PriceConfirmationDeferred",
                    len(current_snapshots),
                )
                return
            finish_price_delivery(
                snapshot_store,
                self._feed.id,
                plan,
                len(current_snapshots),
            )
            return
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        by_id = {pending.product_id: pending for pending in pending_changes}
        changes = self._resolve_changes(
            [by_id[product_id] for product_id in plan.selected_ids],
        )
        unconfirmed = len(changes) != len(plan.selected_ids)
        if unconfirmed and plan.batch_id is not None:
            pause_price_recovery(
                snapshot_store,
                self._feed.id,
                "PriceConfirmationMismatch",
            )
            return
        deliver_price_changes(changes, self._dependencies, self._message_for, plan=plan)
        if unconfirmed or ambiguous_count:
            record_price_health(
                snapshot_store,
                self._feed.id,
                "failed",
                "PriceConfirmationDeferred",
                len(current_snapshots),
            )
        else:
            finish_price_delivery(
                snapshot_store,
                self._feed.id,
                plan,
                len(current_snapshots),
            )

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
        changes: list[_PriceChange] = []
        for pending in pending_changes:
            product = products_by_id.get(pending.product_id)
            if product is None or len(product.variants) != 1:
                continue
            variant = product.variants[0]
            if (
                variant.id != pending.variant_id
                or variant.calculated_price.currency_code.upper()
                != pending.current.currency
                or variant.calculated_price.calculated_amount != pending.current.amount
            ):
                continue
            changes.append(_PriceChange(product, pending.previous, pending.current))
        deferred_count = len(pending_changes) - len(changes)
        if deferred_count:
            logger.warning(
                "Deferred %d unconfirmed price changes for feed %s; "
                "product details must agree with indexed price and variant",
                deferred_count,
                self._feed.id,
            )
        return changes

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

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.title,
                link=f"{SETEC_PRODUCT_BASE_URL}{product.handle}",
                description="",
                author="",
                timestamp=None,
                image_url=product.thumbnail,
                categories=tuple(category.name for category in product.categories),
                source_metrics=self._metrics_for(change),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or SETEC_LABEL,
        )

    @staticmethod
    def _metrics_for(change: _PriceChange) -> tuple[SourceMetric, ...]:
        metrics = [
            SourceMetric(label="Price", value=change.current.formatted),
            SourceMetric(label="Previous", value=change.previous.formatted, prior=True),
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
