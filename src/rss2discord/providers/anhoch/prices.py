"""Sequential selling-price comparison and Discord delivery for one Anhoch feed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from rss2discord.discord.client import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot, SourceMetric
from rss2discord.providers.anhoch.catalog import ANHOCH_LABEL, ANHOCH_PRODUCT_BASE_URL
from rss2discord.providers.anhoch.models import AnhochProduct
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.price_monitor import (
    NO_SNAPSHOT_LIMIT,
    price_direction,
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


class AnhochPriceMonitor(CatalogPriceMonitor[AnhochProduct]):
    """Compare one full catalog against persisted snapshots and alert on changes."""

    provider = "anhoch"
    label = ANHOCH_LABEL

    @property
    def snapshot_limit(self) -> int:
        return NO_SNAPSHOT_LIMIT

    def _snapshot(self, product: AnhochProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.id),
            amount=product.selling_price.amount,
            formatted=product.selling_price.formatted,
            currency=product.selling_price.currency,
            available=product.is_in_stock,
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
