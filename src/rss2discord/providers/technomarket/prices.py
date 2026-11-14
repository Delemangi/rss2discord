"""Opt-in complete-category Technomarket effective-price monitoring."""

from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot, SourceMetric
from rss2discord.providers.technomarket.bounds import (
    MAX_TECHNOMARKET_RETAINED_SNAPSHOTS,
    TECHNOMARKET_LABEL,
)
from rss2discord.providers.technomarket.models import TechnomarketProduct
from rss2discord.providers.technomarket.strategy import (
    TechnomarketStrategy,
    format_technomarket_mkd,
)
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


class TechnomarketCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[TechnomarketProduct, ...]: ...


class TechnomarketPriceMonitor:
    """Compare effective SMART-or-regular prices and persist delivered changes."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[TechnomarketCatalog],
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
        observations: list[PriceObservation[TechnomarketProduct]] = []
        for product in products:
            current = self._snapshot(product)
            if current is not None:
                observations.append(PriceObservation(product, current))
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="technomarket",
            label=TECHNOMARKET_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_TECHNOMARKET_RETAINED_SNAPSHOTS,
        )

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
