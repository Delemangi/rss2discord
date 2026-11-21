"""Generic WooCommerce Store API price monitoring."""

from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_amount import discount_percentage
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
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
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.price_monitor import price_direction

MAX_WOOCOMMERCE_RETAINED_SNAPSHOTS = MAX_WOOCOMMERCE_PRODUCTS


class WooCommerceCatalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WooCommerceProduct, ...]: ...


class WooCommercePriceMonitor(CatalogPriceMonitor[WooCommerceProduct]):
    provider = "woocommerce"
    label = WOOCOMMERCE_LABEL
    empty_catalog_cause = "EmptyCatalog"
    positive_prices_only = True

    @property
    def snapshot_limit(self) -> int:
        return MAX_WOOCOMMERCE_RETAINED_SNAPSHOTS

    def _snapshot(self, product: WooCommerceProduct) -> PriceSnapshot:
        prices = product.prices
        amount = prices.current_amount
        return PriceSnapshot(
            feed_id=self._feed.id,
            product_id=str(product.id),
            amount=amount,
            formatted=format_woocommerce_price(amount, prices.currency_code),
            currency=prices.currency_code,
            available=product.is_in_stock,
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
                discount_percentage=discount_percentage(
                    product.prices.regular_amount,
                    product.prices.current_amount,
                ),
            ),
            source_title=self._feed.name or WOOCOMMERCE_LABEL,
        )
