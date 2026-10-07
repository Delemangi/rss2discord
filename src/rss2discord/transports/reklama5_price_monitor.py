from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
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
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    deliver_planned_price_changes,
    prepare_price_delivery,
    prepare_price_scan,
    price_direction,
    run_price_scan,
)
from rss2discord.transports.reklama5_parser import Reklama5Listing
from rss2discord.transports.reklama5_scope import REKLAMA5_LABEL
from rss2discord.transports.reklama5_strategy import Reklama5Strategy

MAX_REKLAMA5_RETAINED_SNAPSHOTS: Final = 10_000
MAX_REKLAMA5_PRICE_CHANGES_PER_SCAN: Final = 100
_MAX_PRICE_DIGITS: Final = 10
_MKD_PRICE_PATTERN: Final = re.compile(
    r"(?P<amount>(?:\d{1,3}(?:[ .,\u00a0]\d{3})+|\d+))\s*(?:ден\.?|МКД)",
    re.IGNORECASE,
)


class Reklama5Catalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Reklama5Listing, ...]: ...


class Reklama5PriceSnapshotStore(PriceRecoveryStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class Reklama5PriceMonitorDependencies:
    catalog: Reklama5Catalog
    snapshots: Reklama5PriceSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    listing: Reklama5Listing
    previous: PriceSnapshot
    current: PriceSnapshot


class Reklama5PriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: Reklama5PriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        run_price_scan(self._scan, self._dependencies.snapshots, self._feed.id)

    def _scan(self) -> None:
        listings, persisted = prepare_price_scan(
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
                    limit=MAX_REKLAMA5_RETAINED_SNAPSHOTS + 1,
                ),
            ),
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
            snapshot_limit=MAX_REKLAMA5_RETAINED_SNAPSHOTS,
            label=REKLAMA5_LABEL,
        )
        by_listing = {snapshot.product_id: snapshot for snapshot in persisted}
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        current_snapshots: dict[str, PriceSnapshot] = {}
        numeric_listing_ids: set[str] = set()
        for listing in listings:
            current = self._snapshot(listing)
            if current is None:
                continue
            numeric_listing_ids.add(current.product_id)
            current_snapshots[current.product_id] = current
            previous = by_listing.get(current.product_id)
            if previous is None:
                silent_updates.append(current)
            elif (
                previous.amount != current.amount
                or previous.currency != current.currency
            ):
                changes.append(_PriceChange(listing, previous, current))
            elif previous.formatted != current.formatted:
                silent_updates.append(current)
        if len(set(by_listing) | numeric_listing_ids) > MAX_REKLAMA5_RETAINED_SNAPSHOTS:
            raise FeedFetchError(REKLAMA5_LABEL, "SnapshotLimitExceeded")
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        store = self._dependencies.snapshots
        plan = self._dependencies.sqlite_retry_policy.execute(
            lambda: prepare_price_delivery(
                store=store,
                feed_id=self._feed.id,
                provider="reklama5",
                changes=tuple(
                    PriceChangeRecord(c.current.product_id, c.previous, c.current)
                    for c in changes
                ),
                current=current_snapshots,
                persisted=by_listing,
                catalog_count=len(listings),
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

    def _snapshot(self, listing: Reklama5Listing) -> PriceSnapshot | None:
        match = _MKD_PRICE_PATTERN.fullmatch(listing.price.strip())
        if match is None:
            return None
        digits = re.sub(r"[ .,\u00a0]", "", match.group("amount"))
        if len(digits) > _MAX_PRICE_DIGITS:
            return None
        amount = Decimal(digits)
        if amount <= 0:
            return None
        return PriceSnapshot(
            self._feed.id,
            str(listing.entry_id),
            amount,
            listing.price,
            "MKD",
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        base_entry = Reklama5Strategy().get_entry_data(change.listing)
        metrics = (
            SourceMetric("Price", change.current.formatted),
            SourceMetric("Previous", change.previous.formatted, prior=True),
            *(
                metric
                for metric in base_entry.source_metrics
                if metric.label != "Price"
            ),
        )
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                base_entry,
                description="",
                source_metrics=metrics,
                price_direction=price_direction(change.previous, change.current),
            ),
            source_title=self._feed.name or REKLAMA5_LABEL,
        )
