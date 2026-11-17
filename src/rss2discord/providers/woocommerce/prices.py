"""Generic WooCommerce Store API price monitoring."""

from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.providers.woocommerce.bounds import (
    MAX_WOOCOMMERCE_PRODUCTS,
    WOOCOMMERCE_LABEL,
)
from rss2discord.providers.woocommerce.models import (
    WooCommerceProduct,
    first_party_image_url,
)
from rss2discord.providers.woocommerce.strategy import (
    format_woocommerce_price,
    woocommerce_product_metrics,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.price_monitor import (
    PriceMonitorDependencies,
    PriceObservation,
    price_direction,
    run_price_scan,
)

MAX_WOOCOMMERCE_RETAINED_SNAPSHOTS = MAX_WOOCOMMERCE_PRODUCTS


class WooCommerceCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WooCommerceProduct, ...]: ...


class WooCommercePriceMonitor:
    def __init__(
        self,
        feed: FeedConfig,
        dependencies: PriceMonitorDependencies[WooCommerceCatalog],
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
        observations = tuple(
            PriceObservation(product, self._snapshot(product))
            for product in products
            if product.is_in_stock and product.prices.current_amount > 0
        )
        if not observations:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "EmptyCatalog")
        run_price_scan(
            feed=self._feed,
            dependencies=self._dependencies,
            provider="woocommerce",
            label=WOOCOMMERCE_LABEL,
            observations=observations,
            message_for=self._message_for,
            snapshot_limit=MAX_WOOCOMMERCE_RETAINED_SNAPSHOTS,
        )

    def _snapshot(self, product: WooCommerceProduct) -> PriceSnapshot:
        prices = product.prices
        amount = prices.current_amount
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.id),
            amount=amount,
            formatted=format_woocommerce_price(amount, prices.currency_code),
            currency=prices.currency_code,
        )

    def _message_for(
        self,
        product: WooCommerceProduct,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        hostname = urlsplit(product.permalink).hostname
        image_url = (
            first_party_image_url(product, hostname) if hostname is not None else None
        )
        return WebhookMessage(
            feed=self._feed,
            entry=EntryData(
                title=product.name,
                link=product.permalink,
                description="",
                author="",
                timestamp=None,
                image_url=image_url,
                categories=tuple(category.name for category in product.categories),
                source_metrics=woocommerce_product_metrics(
                    product,
                    previous_price=previous.formatted,
                ),
                price_direction=price_direction(previous, current),
            ),
            source_title=self._feed.name or WOOCOMMERCE_LABEL,
        )
