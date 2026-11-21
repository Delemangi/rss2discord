"""Shared scan pipeline for catalog-backed price monitors."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
    PriceObservation,
    run_price_scan,
)


class CatalogPriceMonitor[ProductT](ABC):
    """Fetch a catalog, build observations, and run the shared price scan."""

    provider: str
    label: str
    empty_catalog_cause: str | None = None
    positive_prices_only: bool = False

    @property
    @abstractmethod
    def snapshot_limit(self) -> int:
        """Maximum retained snapshots for this provider."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[Any],
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
        observations = self._observations(products)
        if not observations and self.empty_catalog_cause is not None:
            raise FeedFetchError(self.label, self.empty_catalog_cause)
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider=self.provider,
            label=self.label,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=self.snapshot_limit,
        )

    def _observations(
        self,
        products: Iterable[ProductT],
    ) -> tuple[PriceObservation[ProductT], ...]:
        observations: list[PriceObservation[ProductT]] = []
        for product in products:
            snapshot = self._snapshot(product)
            if snapshot is None:
                continue
            if self.positive_prices_only and snapshot.amount <= 0:
                continue
            observations.append(PriceObservation(product, snapshot))
        return tuple(observations)

    @abstractmethod
    def _snapshot(self, product: ProductT) -> PriceSnapshot | None:
        """Build a persisted snapshot, or None when the product is not priced."""

    @abstractmethod
    def _message_for(
        self,
        product: ProductT,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        """Build one price-change card."""
