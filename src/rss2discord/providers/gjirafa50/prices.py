"""Delivery-safe Gjirafa50 price monitoring."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordSender
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot, SourceMetric
from rss2discord.providers.gjirafa50.catalog import MAX_GJIRAFA50_PRODUCTS
from rss2discord.providers.gjirafa50.models import Gjirafa50Product
from rss2discord.providers.gjirafa50.parser import GJIRAFA50_LABEL
from rss2discord.providers.gjirafa50.strategy import Gjirafa50Strategy
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

MAX_GJIRAFA50_RETAINED_SNAPSHOTS: Final = MAX_GJIRAFA50_PRODUCTS


class Gjirafa50Catalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Gjirafa50Product, ...]: ...


@dataclass(frozen=True, slots=True)
class Gjirafa50PriceMonitorDependencies:
    catalog: Gjirafa50Catalog
    snapshots: PriceRecoveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery
    database_path: Path
    cooldown_peer_feed_ids: tuple[str, ...] = ()


class Gjirafa50PriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: Gjirafa50PriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        observations: list[PriceObservation[Gjirafa50Product]] = []
        for product in products:
            if product.price <= 0:
                continue
            observations.append(
                PriceObservation(
                    product,
                    PriceSnapshot(
                        self._feed.id,
                        str(product.id),
                        product.price,
                        product.formatted_price,
                        product.currency,
                    ),
                ),
            )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="gjirafa50",
            label=GJIRAFA50_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_GJIRAFA50_RETAINED_SNAPSHOTS,
        )

    def _message_for(
        self,
        product: Gjirafa50Product,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                Gjirafa50Strategy().get_entry_data(product),
                description="",
                source_metrics=(
                    SourceMetric("Price", current.formatted),
                    SourceMetric("Previous", previous.formatted, prior=True),
                ),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or GJIRAFA50_LABEL,
        )
