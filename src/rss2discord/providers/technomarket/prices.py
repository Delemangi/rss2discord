"""Opt-in complete-category Technomarket effective-price monitoring."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import SourceMetric
from rss2discord.providers.technomarket.bounds import (
    MAX_TECHNOMARKET_RETAINED_SNAPSHOTS,
    TECHNOMARKET_LABEL,
)
from rss2discord.providers.technomarket.models import TechnomarketProduct
from rss2discord.providers.technomarket.strategy import (
    TechnomarketStrategy,
    format_technomarket_mkd,
)
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
    prepare_price_scan,
    price_direction,
)


class TechnomarketCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[TechnomarketProduct, ...]: ...


class TechnomarketSnapshotStore(PriceRecoveryStore, Protocol):
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
                    limit=MAX_TECHNOMARKET_RETAINED_SNAPSHOTS + 1,
                ),
            ),
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            snapshot_limit=MAX_TECHNOMARKET_RETAINED_SNAPSHOTS,
            label=TECHNOMARKET_LABEL,
        )
        by_id = {snapshot.product_id: snapshot for snapshot in persisted}
        silent: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        current_snapshots: dict[str, PriceSnapshot] = {}
        available_ids: set[str] = set()
        for product in products:
            current = self._snapshot(product)
            if current is None:
                continue
            available_ids.add(current.product_id)
            current_snapshots[current.product_id] = current
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
        store = self._dependencies.snapshots
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=store,
                feed_id=self._feed.id,
                provider="technomarket",
                changes=tuple(
                    PriceChangeRecord(c.current.product_id, c.previous, c.current)
                    for c in changes
                ),
                current=current_snapshots,
                persisted=by_id,
                catalog_count=len(products),
            ),
        )
        if plan.blocked:
            return
        if silent and plan.allow_silent_updates:
            self._dependencies.sqlite_retry_policy.execute(
                lambda: self._dependencies.snapshots.upsert_price_snapshots(silent),
            )
        changes_by_id = {change.current.product_id: change for change in changes}
        deliver_price_changes(
            (changes_by_id[product_id] for product_id in plan.selected_ids),
            self._dependencies,
            self._message_for,
            plan=plan,
        )
        finish_price_delivery(store, self._feed.id, plan, len(current_snapshots))

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
