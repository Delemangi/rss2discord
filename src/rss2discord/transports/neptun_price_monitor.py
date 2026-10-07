"""Delivery-safe actual-price monitoring for one Neptun category."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import SourceMetric
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.neptun import NeptunStrategy, format_neptun_mkd
from rss2discord.transports.neptun_http import NEPTUN_LABEL
from rss2discord.transports.neptun_models import NeptunProduct
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_planned_price_changes,
    diff_price_snapshots,
    prepare_price_delivery,
    prepare_price_scan,
    price_direction,
    run_price_scan,
)

MAX_NEPTUN_RETAINED_SNAPSHOTS: Final = 10_000
MAX_NEPTUN_PRICE_CHANGES_PER_SCAN: Final = 100


class NeptunCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeptunProduct, ...]: ...


class NeptunPriceSnapshotStore(PriceRecoveryStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class NeptunPriceMonitorDependencies:
    catalog: NeptunCatalog
    snapshots: NeptunPriceSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: NeptunProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class NeptunPriceMonitor:
    """Compare positive actual prices and persist changes after Discord delivery."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: NeptunPriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        run_price_scan(self._scan, self._dependencies.snapshots, self._feed.id)

    def _scan(self) -> None:
        products, persisted = prepare_price_scan(
            fetch_products=partial(
                self._dependencies.catalog.fetch_catalog,
                self._feed.url,
                retry_policy=self._dependencies.fetch_retry_policy,
                is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            ),
            load_snapshots=partial(
                self._dependencies.sqlite_retry_policy.execute,
                partial(
                    self._dependencies.snapshots.load_price_snapshots,
                    self._feed.id,
                    limit=MAX_NEPTUN_RETAINED_SNAPSHOTS + 1,
                ),
            ),
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            snapshot_limit=MAX_NEPTUN_RETAINED_SNAPSHOTS,
            label=NEPTUN_LABEL,
        )
        by_product = {snapshot.product_id: snapshot for snapshot in persisted}
        current_snapshots: dict[str, PriceSnapshot] = {}
        products_by_id: dict[str, NeptunProduct] = {}
        for product in products:
            current = self._snapshot(product)
            if current is None:
                continue
            current_snapshots[current.product_id] = current
            products_by_id[current.product_id] = product
        if (
            len(by_product.keys() | current_snapshots.keys())
            > MAX_NEPTUN_RETAINED_SNAPSHOTS
        ):
            raise FeedFetchError(NEPTUN_LABEL, "SnapshotLimitExceeded")
        silent_updates, records = diff_price_snapshots(
            current_snapshots.values(),
            by_product,
        )
        changes = tuple(
            _PriceChange(
                products_by_id[record.product_id],
                record.previous,
                record.current,
            )
            for record in records
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        store = self._dependencies.snapshots
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=store,
                feed_id=self._feed.id,
                provider="neptun",
                changes=tuple(
                    PriceChangeRecord(c.current.product_id, c.previous, c.current)
                    for c in changes
                ),
                current=current_snapshots,
                persisted=by_product,
                catalog_count=len(products),
            ),
        )
        deliver_planned_price_changes(
            self._dependencies,
            self._message_for,
            feed_id=self._feed.id,
            plan=plan,
            silent_updates=silent_updates,
            changes=changes,
            item_count=len(current_snapshots),
        )

    def _snapshot(self, product: NeptunProduct) -> PriceSnapshot | None:
        if product.actual_price <= 0:
            return None
        return PriceSnapshot(
            self._feed.id,
            str(product.id),
            product.actual_price,
            format_neptun_mkd(product.actual_price),
            "MKD",
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        base_entry = NeptunStrategy().get_entry_data(change.product)
        metrics = [
            SourceMetric("Price", change.current.formatted),
            SourceMetric("Previous", change.previous.formatted, prior=True),
        ]
        if change.product.regular_price != change.product.actual_price:
            metrics.append(
                SourceMetric(
                    "Original",
                    format_neptun_mkd(change.product.regular_price),
                ),
            )
        metrics.extend(
            (
                SourceMetric("Manufacturer", change.product.manufacturer.name),
                SourceMetric("Code", change.product.code_number),
                SourceMetric(
                    "Online",
                    "Available"
                    if change.product.available_online
                    and change.product.available_webshop
                    else "Unavailable",
                ),
            ),
        )
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                base_entry,
                description="",
                source_metrics=tuple(metrics),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or NEPTUN_LABEL,
        )
