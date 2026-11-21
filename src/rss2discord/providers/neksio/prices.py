"""Sequential tax-inclusive price comparison and Discord delivery for Neksio."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from typing import Final, Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import WebhookMessage
from rss2discord.models import PriceSnapshot, SourceMetric
from rss2discord.providers.neksio.client import NEKSIO_LABEL
from rss2discord.providers.neksio.models import NeksioProduct
from rss2discord.providers.neksio.strategy import _entry_data
from rss2discord.retries import (
    FeedFetchInterruptedError,
)
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
    PriceObservation,
    price_direction,
    run_price_scan,
)

MAX_NEKSIO_RETAINED_SNAPSHOTS: Final = 50_000


class NeksioCatalog(Protocol):
    """Retrieve a validated full Neksio catalog in API order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[NeksioProduct, ...]: ...


class NeksioPriceMonitor:
    """Compare one full catalog against persisted snapshots and alert on changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[NeksioCatalog],
    ) -> None:
        self._feed: FeedConfig = feed
        self._dependencies: PriceMonitorDependencies[NeksioCatalog] = dependencies

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        """Fetch, classify, persist silent updates, then deliver changed prices in order."""
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.fetch_retry_policy.execute(
            lambda: self._dependencies.catalog.fetch_catalog(
                self._feed.url,
                is_shutdown_requested=(
                    self._dependencies.delivery.is_shutdown_requested
                ),
            ),
        )
        observations = tuple(
            PriceObservation(product, self._snapshot(product)) for product in products
        )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="neksio",
            label=NEKSIO_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_NEKSIO_RETAINED_SNAPSHOTS,
        )

    def _snapshot(self, product: NeksioProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.product_id),
            amount=Decimal(str(product.price_with_tax)),
            formatted=product.formatted_price,
            currency="MKD",
        )

    def _message_for(
        self,
        product: NeksioProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                _entry_data(product),
                source_metrics=self._metrics_for(product, previous, current),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or NEKSIO_LABEL,
        )

    @staticmethod
    def _metrics_for(
        product: NeksioProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> tuple[SourceMetric, ...]:
        metrics = [
            SourceMetric(label="Price", value=current.formatted),
            SourceMetric(label="Previous", value=previous.formatted, prior=True),
        ]
        if product.old_formatted_price:
            metrics.append(
                SourceMetric(label="Original", value=product.old_formatted_price),
            )
        if product.product_code:
            metrics.append(
                SourceMetric(label="Product code", value=product.product_code),
            )
        if product.manufacturer:
            metrics.append(
                SourceMetric(label="Manufacturer", value=product.manufacturer),
            )
        metrics.append(SourceMetric(label="Stock", value=str(product.stock_quantity)))
        return tuple(metrics)
