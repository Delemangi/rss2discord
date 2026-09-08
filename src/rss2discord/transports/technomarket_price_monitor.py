"""Opt-in complete-category Technomarket effective-price monitoring."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import SourceMetric
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceSnapshotStore,
    deliver_price_changes,
    price_direction,
)
from rss2discord.transports.technomarket import (
    TechnomarketStrategy,
    format_technomarket_mkd,
)
from rss2discord.transports.technomarket_bounds import (
    MAX_TECHNOMARKET_PRICE_CHANGES_PER_SCAN,
    MAX_TECHNOMARKET_RETAINED_SNAPSHOTS,
    TECHNOMARKET_LABEL,
)
from rss2discord.transports.technomarket_models import TechnomarketProduct


class TechnomarketCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[TechnomarketProduct, ...]: ...


class TechnomarketSnapshotStore(PriceSnapshotStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class TechnomarketPriceMonitorDependencies:
    catalog: TechnomarketCatalog
    snapshots: TechnomarketSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    product: TechnomarketProduct
    previous: PriceSnapshot
    current: PriceSnapshot


class TechnomarketPriceMonitor:
    """Compare effective SMART-or-regular prices and persist delivered changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: TechnomarketPriceMonitorDependencies,
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
                limit=MAX_TECHNOMARKET_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_TECHNOMARKET_RETAINED_SNAPSHOTS:
            raise FeedFetchError(TECHNOMARKET_LABEL, "SnapshotLimitExceeded")
        by_id = {snapshot.product_id: snapshot for snapshot in persisted}
        silent: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        available_ids: set[str] = set()
        for product in products:
            current = self._snapshot(product)
            if current is None:
                continue
            available_ids.add(current.product_id)
            previous = by_id.get(current.product_id)
            if previous is None:
                silent.append(current)
            elif (
                previous.amount != current.amount
                or previous.currency != current.currency
            ):
                changes.append(_PriceChange(product, previous, current))
            elif previous.formatted != current.formatted:
                silent.append(current)
        if len(set(by_id) | available_ids) > MAX_TECHNOMARKET_RETAINED_SNAPSHOTS:
            raise FeedFetchError(TECHNOMARKET_LABEL, "SnapshotLimitExceeded")
        if len(changes) > MAX_TECHNOMARKET_PRICE_CHANGES_PER_SCAN:
            raise FeedFetchError(TECHNOMARKET_LABEL, "PriceChangeLimitExceeded")
        if silent:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(silent),
            )
        deliver_price_changes(changes, self._dependencies, self._message_for)

    def _snapshot(self, product: TechnomarketProduct) -> PriceSnapshot | None:
        amount = product.effective_price
        if amount is None or amount <= 0:
            return None
        return PriceSnapshot(
            self._feed.id,
            product.product_id,
            amount,
            format_technomarket_mkd(amount),
            "MKD",
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        product = change.product
        base = TechnomarketStrategy().get_entry_data(product)
        metrics = [
            SourceMetric("Price", change.current.formatted),
            SourceMetric("Previous", change.previous.formatted, prior=True),
        ]
        if (
            product.regular_price is not None
            and product.regular_price != product.effective_price
        ):
            metrics.append(
                SourceMetric(
                    "Original",
                    format_technomarket_mkd(product.regular_price),
                ),
            )
        if product.manufacturer:
            metrics.append(SourceMetric("Manufacturer", product.manufacturer))
        metrics.extend(
            SourceMetric("Category", category) for category in product.categories
        )
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                base,
                source_metrics=tuple(metrics),
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or TECHNOMARKET_LABEL,
        )
