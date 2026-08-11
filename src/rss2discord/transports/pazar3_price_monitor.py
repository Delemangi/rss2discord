from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
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
from rss2discord.transports.pazar3 import Pazar3Strategy
from rss2discord.transports.pazar3_models import Pazar3Listing
from rss2discord.transports.pazar3_scope import PAZAR3_LABEL
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceSnapshotStore,
    deliver_price_changes,
    price_direction,
)

MAX_PAZAR3_RETAINED_SNAPSHOTS: Final = 10_000
MAX_PAZAR3_PRICE_CHANGES_PER_SCAN: Final = 100
_MAX_PRICE_DIGITS: Final = 10
_PRICE_PATTERN: Final = re.compile(
    r"(?P<amount>(?:\d{1,3}(?:[ .,\u00a0]\d{3})+|\d+))\s*"
    r"(?P<currency>ден\.?|МКД|MKD|EUR|ЕУР|€)",
    re.IGNORECASE,
)


class Pazar3Catalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Pazar3Listing, ...]: ...


class Pazar3PriceSnapshotStore(PriceSnapshotStore, Protocol):
    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...


@dataclass(frozen=True, slots=True)
class Pazar3PriceMonitorDependencies:
    catalog: Pazar3Catalog
    snapshots: Pazar3PriceSnapshotStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


@dataclass(frozen=True, slots=True)
class _PriceChange:
    listing: Pazar3Listing
    previous: PriceSnapshot
    current: PriceSnapshot


class Pazar3PriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: Pazar3PriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        listings = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        persisted = self._dependencies.sqlite_retry_policy.execute(
            lambda: self._dependencies.snapshots.load_price_snapshots(
                self._feed.id,
                limit=MAX_PAZAR3_RETAINED_SNAPSHOTS + 1,
            ),
        )
        if len(persisted) > MAX_PAZAR3_RETAINED_SNAPSHOTS:
            raise FeedFetchError(PAZAR3_LABEL, "SnapshotLimitExceeded")
        by_listing = {snapshot.product_id: snapshot for snapshot in persisted}
        silent_updates: list[PriceSnapshot] = []
        changes: list[_PriceChange] = []
        valid_listing_ids: set[str] = set()
        for listing in listings:
            current = self._snapshot(listing)
            if current is None:
                continue
            valid_listing_ids.add(current.product_id)
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
        if len(set(by_listing) | valid_listing_ids) > MAX_PAZAR3_RETAINED_SNAPSHOTS:
            raise FeedFetchError(PAZAR3_LABEL, "SnapshotLimitExceeded")
        if len(changes) > MAX_PAZAR3_PRICE_CHANGES_PER_SCAN:
            raise FeedFetchError(PAZAR3_LABEL, "PriceChangeLimitExceeded")
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
        deliver_price_changes(changes, self._dependencies, self._message_for)

    def _snapshot(self, listing: Pazar3Listing) -> PriceSnapshot | None:
        match = _PRICE_PATTERN.fullmatch(listing.price.strip())
        if match is None:
            return None
        digits = re.sub(r"[ .,\u00a0]", "", match.group("amount"))
        if len(digits) > _MAX_PRICE_DIGITS:
            return None
        amount = Decimal(digits)
        if amount <= 0:
            return None
        currency_token = match.group("currency").casefold()
        currency = "EUR" if currency_token in {"eur", "еур", "€"} else "MKD"
        return PriceSnapshot(
            self._feed.id,
            str(listing.entry_id),
            amount,
            listing.price,
            currency,
        )

    def _message_for(self, change: _PriceChange) -> WebhookMessage:
        base_entry = Pazar3Strategy().get_entry_data(change.listing)
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
            source_title=self._feed.name or PAZAR3_LABEL,
        )
