"""Sequential selling-price comparison and Discord delivery for one Anhoch feed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot, SourceMetric
from rss2discord.providers.anhoch.catalog import ANHOCH_LABEL, ANHOCH_PRODUCT_BASE_URL
from rss2discord.providers.anhoch.models import AnhochProduct
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    NO_SNAPSHOT_LIMIT,
    PriceMonitorDependencies,
    PriceObservation,
    price_direction,
    run_price_scan,
)


class AnhochCatalog(Protocol):
    """Retrieve a validated full Anhoch catalog in API order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnhochProduct, ...]: ...


class AnhochPriceMonitor:
    """Compare one full catalog against persisted snapshots and alert on changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[AnhochCatalog],
    ) -> None:
        self._feed: FeedConfig = feed
        self._dependencies: PriceMonitorDependencies[AnhochCatalog] = dependencies

    def scan(self) -> None:
        self._scan()

    def _scan(self) -> None:
        """Fetch, classify, persist silent updates, then deliver changed prices in order."""
        if self._dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
        products = self._dependencies.catalog.fetch_catalog(
            self._feed.url,
            retry_policy=self._dependencies.fetch_retry_policy,
            is_shutdown_requested=self._dependencies.delivery.is_shutdown_requested,
        )
        observations = tuple(
            PriceObservation(product, self._snapshot(product)) for product in products
        )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="anhoch",
            label=ANHOCH_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=NO_SNAPSHOT_LIMIT,
        )

    def _snapshot(self, product: AnhochProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.id),
            amount=product.selling_price.amount,
            formatted=product.selling_price.formatted,
            currency=product.selling_price.currency,
        )

    def _message_for(
        self,
        product: AnhochProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=f"{ANHOCH_PRODUCT_BASE_URL}{product.slug}",
                description="",
                author="",
                timestamp=None,
                image_url=(
                    product.base_image.path if product.base_image is not None else None
                ),
                source_metrics=self._metrics_for(product, previous, current),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or ANHOCH_LABEL,
        )

    @staticmethod
    def _metrics_for(
        product: AnhochProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> tuple[SourceMetric, ...]:
        metrics = [
            SourceMetric(label="Price", value=current.formatted),
            SourceMetric(label="Previous", value=previous.formatted, prior=True),
        ]
        if product.price.formatted != product.selling_price.formatted:
            metrics.append(
                SourceMetric(label="Original", value=product.price.formatted),
            )
        stock = (
            str(product.qty) if product.is_in_stock and product.qty is not None else "0"
        )
        metrics.append(SourceMetric(label="Stock", value=stock))
        if product.installments is not None:
            metrics.append(
                SourceMetric(
                    label="Installments",
                    value=(
                        f"{product.installments.period} × "
                        f"{product.installments.price.formatted}"
                    ),
                ),
            )
        return tuple(metrics)
