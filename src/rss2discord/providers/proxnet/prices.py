"""Opt-in PROXNet category price monitoring."""

from collections.abc import Callable, Iterable
from typing import Protocol

from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.providers.proxnet.bounds import (
    MAX_PROXNET_RETAINED_SNAPSHOTS,
    PROXNET_LABEL,
)
from rss2discord.providers.proxnet.models import ProxnetProduct
from rss2discord.providers.proxnet.strategy import ProxnetStrategy
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceObservation,
    build_price_change_message,
)


class ProxnetCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[ProxnetProduct, ...]: ...


class ProxnetPriceMonitor(CatalogPriceMonitor[ProxnetProduct]):
    """Compare one complete PROXNet category against persisted snapshots."""

    provider = "proxnet"
    label = PROXNET_LABEL

    @property
    def snapshot_limit(self) -> int:
        return MAX_PROXNET_RETAINED_SNAPSHOTS

    def _observations(
        self,
        products: Iterable[ProxnetProduct],
    ) -> tuple[PriceObservation[ProxnetProduct], ...]:
        return tuple(
            PriceObservation(product, self._snapshot(product))
            for product in self._priced_products(tuple(products))
        )

    @staticmethod
    def _priced_products(
        products: tuple[ProxnetProduct, ...],
    ) -> tuple[ProxnetProduct, ...]:
        """Keep the first observation per ID and only positive MKD prices."""
        seen: set[str] = set()
        priced: list[ProxnetProduct] = []
        for product in products:
            if product.product_id in seen:
                continue
            seen.add(product.product_id)
            if product.currency != "MKD" or product.amount <= 0:
                continue
            priced.append(product)
        return tuple(priced)

    def _snapshot(self, product: ProxnetProduct) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=product.product_id,
            amount=product.amount,
            formatted=product.formatted_price,
            currency=product.currency,
        )

    def _message_for(
        self,
        product: ProxnetProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return build_price_change_message(
            feed=self._feed,
            base_entry=ProxnetStrategy().get_entry_data(product),
            previous=previous,
            current=current,
            source_title=self._feed.name or PROXNET_LABEL,
        )
