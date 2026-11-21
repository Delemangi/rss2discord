"""Opt-in Ananas catalog price monitoring."""

from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

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
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.price_monitor import price_direction


class AnanasCatalog(Protocol):
    """Retrieve a validated grouped Ananas catalog."""

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnanasProduct, ...]: ...


class AnanasPriceMonitor(CatalogPriceMonitor[AnanasProduct]):
    """Compare in-stock Ananas offers against persisted slug snapshots."""

    provider = "ananas"
    label = ANANAS_LABEL
    positive_prices_only = True

    @property
    def snapshot_limit(self) -> int:
        return MAX_ANANAS_RETAINED_SNAPSHOTS

    def _snapshot(self, product: AnanasProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.product_id,
            amount=Decimal(product.price),
            formatted=format_ananas_mkd(product.price),
            currency=product.currency,
            available=product.in_stock,
        )

    def _message_for(
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
