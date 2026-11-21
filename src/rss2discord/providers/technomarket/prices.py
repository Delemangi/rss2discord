"""Opt-in complete-category Technomarket effective-price monitoring."""

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot, SourceMetric
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.providers.technomarket.bounds import (
    MAX_TECHNOMARKET_RETAINED_SNAPSHOTS,
    TECHNOMARKET_LABEL,
)
from rss2discord.providers.technomarket.models import TechnomarketProduct
from rss2discord.providers.technomarket.strategy import (
    TechnomarketStrategy,
    format_technomarket_mkd,
)
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.price_monitor import price_direction


class TechnomarketCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[TechnomarketProduct, ...]: ...


class TechnomarketPriceMonitor(CatalogPriceMonitor[TechnomarketProduct]):
    """Compare effective SMART-or-regular prices and persist delivered changes."""

    provider = "technomarket"
    label = TECHNOMARKET_LABEL

    @property
    def snapshot_limit(self) -> int:
        return MAX_TECHNOMARKET_RETAINED_SNAPSHOTS

    def _snapshot(self, product: TechnomarketProduct) -> PriceSnapshot | None:
        amount = product.effective_price
        if amount is None or amount <= 0:
            return None
        return PriceSnapshot(
            self._feed.id,
            product.product_id,
            amount,
            format_technomarket_mkd(amount),
            "MKD",
        )

    def _message_for(
        self,
        product: TechnomarketProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        base = TechnomarketStrategy().get_entry_data(product)
        metrics = [
            SourceMetric("Price", current.formatted),
            SourceMetric("Previous", previous.formatted, prior=True),
        ]
        if (
            product.regular_price is not None
            and product.regular_price != product.effective_price
        ):
            metrics.append(
                SourceMetric(
                    "Original",
                    format_technomarket_mkd(product.regular_price),
                ),
            )
        if product.manufacturer:
            metrics.append(SourceMetric("Manufacturer", product.manufacturer))
        metrics.extend(
            SourceMetric("Category", category) for category in product.categories
        )
        return WebhookMessage(
            feed=self._feed,
            entry=replace(
                base,
                source_metrics=tuple(metrics),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or TECHNOMARKET_LABEL,
        )
