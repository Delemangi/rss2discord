from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot, SourceMetric
from rss2discord.providers.reklama5.parser import Reklama5Listing
from rss2discord.providers.reklama5.scope import REKLAMA5_LABEL
from rss2discord.providers.reklama5.strategy import Reklama5Strategy
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
    PriceObservation,
    price_direction,
    run_price_scan,
)

MAX_REKLAMA5_RETAINED_SNAPSHOTS: Final = 10_000
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


class Reklama5PriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[Reklama5Catalog],
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
        observations: list[PriceObservation[Reklama5Listing]] = []
        for listing in listings:
            current = self._snapshot(listing)
            if current is not None:
                observations.append(PriceObservation(listing, current))
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="reklama5",
            label=REKLAMA5_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_REKLAMA5_RETAINED_SNAPSHOTS,
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

    def _message_for(
        self,
        listing: Reklama5Listing,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        base_entry = Reklama5Strategy().get_entry_data(listing)
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
            source_title=self._feed.name or REKLAMA5_LABEL,
        )
