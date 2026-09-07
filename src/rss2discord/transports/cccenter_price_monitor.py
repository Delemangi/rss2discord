"""Opt-in CCCenter catalog price monitoring."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, assert_never

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
from rss2discord.transports.cccenter import format_cccenter_mkd
from rss2discord.transports.cccenter_bounds import (
    CCCENTER_LABEL,
    MAX_CCCENTER_PRICE_CHANGES_PER_SCAN,
    MAX_CCCENTER_RETAINED_SNAPSHOTS,
)
from rss2discord.transports.cccenter_models import CCCenterProduct
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceSnapshotStore,
    price_direction,
)


class CCCenterCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[CCCenterProduct, ...]: ...


class CCCenterSnapshotStore(PriceSnapshotStore, Protocol):
    def load_price_snapshots(self, feed_id: str, *, limit: int | None = None) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class CCCenterPriceMonitorDependencies:
    catalog: CCCenterCatalog
    snapshots: CCCenterSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: CCCenterProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class CCCenterPriceMonitor:
    def __init__(self, feed: FeedConfig, dependencies: CCCenterPriceMonitorDependencies) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        snapshots = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id, limit=MAX_CCCENTER_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(snapshots) > MAX_CCCENTER_RETAINED_SNAPSHOTS:
            raise FeedFetchError(CCCENTER_LABEL, "SnapshotLimitExceeded")
        by_id = {snapshot.product_id: snapshot for snapshot in snapshots}
        available = tuple(
            product
            for product in products
            if (
                product.price_status == "scalar"
                and product.current_price is not None
                and product.current_price > 0
            )
        )
        if len(set(by_id).union(product.product_id for product in available)) > MAX_CCCENTER_RETAINED_SNAPSHOTS:
            raise FeedFetchError(CCCENTER_LABEL, "SnapshotLimitExceeded")
        silent: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        for product in available:
            current = self._snapshot(product)
            previous = by_id.get(product.product_id)
            if previous is None:
                silent.append(current)
            elif previous.amount != current.amount or previous.currency != current.currency:
                changes.append(_PriceChange(product, previous, current))
            elif previous.formatted != current.formatted:
                silent.append(current)
        if len(changes) > MAX_CCCENTER_PRICE_CHANGES_PER_SCAN:
            raise FeedFetchError(CCCENTER_LABEL, "PriceChangeLimitExceeded")
        if silent:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(silent),
            )
        self._deliver(changes)

    def _snapshot(self, product: CCCenterProduct) -> PriceSnapshot:
        if product.current_price is None:
            raise FeedFetchError(CCCENTER_LABEL, "UnavailablePrice")
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.product_id,
            amount=product.current_price,
            formatted=format_cccenter_mkd(product.current_price),
            currency="MKD",
        )

    def _deliver(self, changes: list[_PriceChange]) -> None:
        delay = False
        for change in changes:
            if self._dependencies.delivery.is_shutdown_requested():
                return
            if delay and self._dependencies.delivery.delay_between_posts > 0 and not self._dependencies.delivery.sleep(self._dependencies.delivery.delay_between_posts):
                return
            result = self._dependencies.sender.send(
                self._message(change), self._dependencies.delivery.sleep,
            )
            match result:
                case DiscordDeliveryResult.DELIVERED:
                    self._dependencies.sqlite_retry_policy.execute(
                        partial(self._persist_snapshot, change.current),
                    )
                    delay = True
                case DiscordDeliveryResult.FAILED:
                    delay = False
                case DiscordDeliveryResult.INTERRUPTED:
                    return
                case other:
                    assert_never(other)

    def _persist_snapshot(self, snapshot: PriceSnapshot) -> None:
        self._dependencies.snapshots.upsert_price_snapshot(snapshot)

    def _message(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.url,
                description="",
                author="",
                timestamp=None,
                image_url=product.image_url,
                categories=product.categories,
                source_metrics=(
                    SourceMetric("Price", format_cccenter_mkd(product.current_price))
                    if product.current_price is not None
                    else SourceMetric("Price", "Unavailable"),
                    SourceMetric("Previous", change.previous.formatted, prior=True),
                    *(
                        (SourceMetric("Original", format_cccenter_mkd(product.original_price)),)
                        if product.original_price is not None
                        and product.original_price != product.current_price
                        else ()
                    ),
                    SourceMetric("Stock", "In stock" if product.is_in_stock else "Out of stock"),
                    *( (SourceMetric("SKU", product.sku),) if product.sku else () ),
                ),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or CCCENTER_LABEL,
        )
