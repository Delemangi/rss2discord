"""Delivery-safe Gjirafa50 price monitoring."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import SourceMetric
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.gjirafa50 import Gjirafa50Strategy
from rss2discord.transports.gjirafa50_models import Gjirafa50Product
from rss2discord.transports.gjirafa50_parser import GJIRAFA50_LABEL
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceSnapshotStore,
    deliver_price_changes,
)

MAX_GJIRAFA50_RETAINED_SNAPSHOTS: Final = 100_000
MAX_GJIRAFA50_PRICE_CHANGES_PER_SCAN: Final = 100


class Gjirafa50Catalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Gjirafa50Product, ...]: ...


class Gjirafa50PriceSnapshotStore(PriceSnapshotStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class Gjirafa50PriceMonitorDependencies:
    catalog: Gjirafa50Catalog
    snapshots: Gjirafa50PriceSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery
    database_path: Path


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: Gjirafa50Product
    previous: PriceSnapshot
    current: PriceSnapshot


class Gjirafa50PriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: Gjirafa50PriceMonitorDependencies,
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
                limit=MAX_GJIRAFA50_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_GJIRAFA50_RETAINED_SNAPSHOTS:
            raise FeedFetchError(GJIRAFA50_LABEL, "SnapshotLimitExceeded")
        by_product = {snapshot.product_id: snapshot for snapshot in persisted}
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        current_ids: set[str] = set()
        for product in products:
            if product.price <= 0:
                continue
            current = PriceSnapshot(
                self._feed.id,
                str(product.id),
                product.price,
                product.formatted_price,
                "MKD",
            )
            current_ids.add(current.product_id)
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
        if len(set(by_product) | current_ids) > MAX_GJIRAFA50_RETAINED_SNAPSHOTS:
            raise FeedFetchError(GJIRAFA50_LABEL, "SnapshotLimitExceeded")
        if len(changes) > MAX_GJIRAFA50_PRICE_CHANGES_PER_SCAN:
            raise FeedFetchError(GJIRAFA50_LABEL, "PriceChangeLimitExceeded")
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(
                    silent_updates,
                ),
            )
        deliver_price_changes(changes, self._dependencies, self._message_for)

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        action = "changed"
        if change.previous.currency == change.current.currency:
            action = (
                "decreased"
                if change.current.amount < change.previous.amount
                else "increased"
            )
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                Gjirafa50Strategy().get_entry_data(change.product),
                description=(
                    f"Price {action} from {change.previous.formatted} "
                    f"to {change.current.formatted}"
                ),
                source_metrics=(
                    SourceMetric("Price", change.current.formatted),
                    SourceMetric("Previous", change.previous.formatted),
                ),
            ),
            source_title=self._feed.name or GJIRAFA50_LABEL,
        )
