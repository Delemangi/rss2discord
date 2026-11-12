from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import SourceMetric
from rss2discord.providers.pazar3.models import Pazar3Listing
from rss2discord.providers.pazar3.scope import PAZAR3_LABEL
from rss2discord.providers.pazar3.strategy import Pazar3Strategy
from rss2discord.recovery_models import PriceSnapshot
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceObservation,
    PriceRecoveryStore,
    price_direction,
    run_price_scan,
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


class Pazar3PriceSnapshotStore(PriceRecoveryStore, Protocol):
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


class Pazar3PriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: Pazar3PriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        listings = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        observations: list[PriceObservation[Pazar3Listing]] = []
        for listing in listings:
            current = self._snapshot(listing)
            if current is not None:
                observations.append(PriceObservation(listing, current))
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="pazar3",
            label=PAZAR3_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_PAZAR3_RETAINED_SNAPSHOTS,
        )

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

    def _message_for(
        self,
        listing: Pazar3Listing,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        base_entry = Pazar3Strategy().get_entry_data(listing)
        metrics = (
            SourceMetric("Price", current.formatted),
            SourceMetric("Previous", previous.formatted, prior=True),
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
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or PAZAR3_LABEL,
        )
