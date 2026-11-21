"""Opt-in Ananas catalog price monitoring."""

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot, SourceMetric
from rss2discord.price_amount import discount_percentage
from rss2discord.providers.ananas.bounds import (
    ANANAS_LABEL,
    MAX_ANANAS_RETAINED_SNAPSHOTS,
)
from rss2discord.providers.ananas.models import AnanasProduct
from rss2discord.providers.ananas.strategy import (
    format_ananas_mkd,
    format_ananas_timestamp,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
    PriceObservation,
    price_direction,
    run_price_scan,
)


class AnanasCatalog(Protocol):
    """Retrieve a validated grouped Ananas catalog."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnanasProduct, ...]: ...


class AnanasPriceMonitor:
    """Compare in-stock Ananas offers against persisted slug snapshots."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[AnanasCatalog],
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        started_at = time.perf_counter()
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        # Out-of-stock products with a valid price are still observed so a later
        # restock can be detected; non-positive prices are never observed.
        observations = tuple(
            PriceObservation(product, self._snapshot(product))
            for product in products
            if product.price > 0
        )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="ananas",
            label=ANANAS_LABEL,
            observations=observations,
            message_for=self._message,
            snapshot_limit=MAX_ANANAS_RETAINED_SNAPSHOTS,
            started_at=started_at,
        )

    def _snapshot(self, product: AnanasProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.product_id,
            amount=Decimal(product.price),
            formatted=format_ananas_mkd(product.price),
            currency=product.currency,
            available=product.in_stock,
        )

    def _message(
        self,
        product: AnanasProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        metrics: list[SourceMetric] = [
            SourceMetric("Price", current.formatted),
            SourceMetric("Previous", previous.formatted, prior=True),
        ]
        if product.base_price is not None and product.base_price != product.price:
            metrics.append(
                SourceMetric("Original", format_ananas_mkd(product.base_price)),
            )
        metrics.append(
            SourceMetric("Stock", "In stock" if product.in_stock else "Out of stock"),
        )
        metrics.append(SourceMetric("Merchant", product.merchant))
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.url,
                description="",
                author="",
                timestamp=format_ananas_timestamp(product.first_published_ms),
                image_url=product.image_url,
                categories=product.categories,
                source_metrics=tuple(metrics),
                price_direction=price_direction(previous, current),
                discount_percentage=(
                    discount_percentage(product.base_price, product.price)
                    if product.base_price is not None
                    else None
                ),
            ),
            source_title=self._feed.name or ANANAS_LABEL,
        )
