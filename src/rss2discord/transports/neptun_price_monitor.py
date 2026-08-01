"""Delivery-safe actual-price monitoring for one Neptun category."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Final, Protocol, assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordDeliveryResult, DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import SourceMetric
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.neptun import NeptunStrategy, format_neptun_mkd
from rss2discord.transports.neptun_http import NEPTUN_LABEL
from rss2discord.transports.neptun_models import NeptunProduct
from rss2discord.transports.price_monitor import PriceAlertDelivery, PriceSnapshotStore

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


class NeptunPriceSnapshotStore(PriceSnapshotStore, Protocol):
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
                limit=MAX_NEPTUN_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_NEPTUN_RETAINED_SNAPSHOTS:
            raise FeedFetchError(NEPTUN_LABEL, "SnapshotLimitExceeded")
        by_product = {snapshot.product_id: snapshot for snapshot in persisted}
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        positive_product_ids: set[str] = set()
        for product in products:
            current = self._snapshot(product)
            if current is None:
                continue
            positive_product_ids.add(current.product_id)
            previous = by_product.get(current.product_id)
            if previous is None:
                silent_updates.append(current)
            elif (
                previous.amount != current.amount
                or previous.currency != current.currency
            ):
                changes.append(_PriceChange(product, previous, current))
            elif previous.formatted != current.formatted:
                silent_updates.append(current)
        if len(set(by_product) | positive_product_ids) > MAX_NEPTUN_RETAINED_SNAPSHOTS:
            raise FeedFetchError(NEPTUN_LABEL, "SnapshotLimitExceeded")
        if len(changes) > MAX_NEPTUN_PRICE_CHANGES_PER_SCAN:
            raise FeedFetchError(NEPTUN_LABEL, "PriceChangeLimitExceeded")
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
        delay_before_next = False
        for change in changes:
            if self._dependencies.delivery.is_shutdown_requested():
                return
            if (
                delay_before_next
                and self._dependencies.delivery.delay_between_posts > 0
                and not self._dependencies.delivery.sleep(
                    self._dependencies.delivery.delay_between_posts,
                )
            ):
                return
            delay_before_next = False
            result = self._dependencies.sender.send(
                self._message_for(change),
                self._dependencies.delivery.sleep,
            )
            match result:
                case DiscordDeliveryResult.DELIVERED:
                    self._dependencies.sqlite_retry_policy.execute(
                        partial(
                            self._dependencies.snapshots.upsert_price_snapshot,
                            change.current,
                        ),
                    )
                    delay_before_next = True
                case DiscordDeliveryResult.FAILED:
                    if self._dependencies.delivery.is_shutdown_requested():
                        return
                case DiscordDeliveryResult.INTERRUPTED:
                    return
                case unreachable:
                    assert_never(unreachable)

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
        action = "changed"
        if change.previous.currency == change.current.currency:
            action = (
                "decreased"
                if change.current.amount < change.previous.amount
                else "increased"
            )
        metrics = [
            SourceMetric("Price", change.current.formatted),
            SourceMetric("Previous", change.previous.formatted),
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
                description=(
                    f"Price {action} from {change.previous.formatted} "
                    f"to {change.current.formatted}"
                ),
                source_metrics=tuple(metrics),
            ),
            source_title=self._feed.name or NEPTUN_LABEL,
        )
