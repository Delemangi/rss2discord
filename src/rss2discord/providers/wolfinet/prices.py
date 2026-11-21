"""Opt-in Wolfinet shop price monitoring."""

import time
from collections.abc import Callable
from typing import Protocol

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot
from rss2discord.providers.wolfinet.bounds import (
    MAX_WOLFINET_RETAINED_SNAPSHOTS,
    WOLFINET_LABEL,
)
from rss2discord.providers.wolfinet.models import WolfinetProduct
from rss2discord.providers.wolfinet.strategy import WolfinetStrategy
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
    PriceObservation,
    build_price_change_message,
    run_price_scan,
)


class WolfinetCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WolfinetProduct, ...]: ...


class WolfinetPriceMonitor:
    """Compare the complete Wolfinet shop against persisted snapshots."""

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[WolfinetCatalog],
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
        observations = tuple(
            PriceObservation(product, self._snapshot(product))
            for product in self._priced_products(products)
        )
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="wolfinet",
            label=WOLFINET_LABEL,
            observations=observations,
            message_for=self.message_for,
            snapshot_limit=MAX_WOLFINET_RETAINED_SNAPSHOTS,
            started_at=started_at,
        )

    @staticmethod
    def _priced_products(
        products: tuple[WolfinetProduct, ...],
    ) -> tuple[WolfinetProduct, ...]:
        """Keep the first observation per ID and only positive MKD prices."""
        seen: set[str] = set()
        priced: list[WolfinetProduct] = []
        for product in products:
            if product.product_id in seen:
                continue
            seen.add(product.product_id)
            if product.currency != "MKD" or product.amount <= 0:
                continue
            priced.append(product)
        return tuple(priced)

    def _snapshot(self, product: WolfinetProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.product_id,
            amount=product.amount,
            formatted=product.formatted_price,
            currency=product.currency,
        )

    def message_for(
        self,
        product: WolfinetProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return build_price_change_message(
            feed=self._feed,
            base_entry=WolfinetStrategy().get_entry_data(product),
            previous=previous,
            current=current,
            source_title=self._feed.name or WOLFINET_LABEL,
        )
