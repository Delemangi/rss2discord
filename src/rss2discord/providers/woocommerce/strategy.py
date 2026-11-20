"""Generic WooCommerce Store API discovery strategy."""

from collections.abc import Callable
from decimal import Decimal
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount, discount_percentage
from rss2discord.providers.woocommerce.bounds import WOOCOMMERCE_LABEL
from rss2discord.providers.woocommerce.catalog import WooCommerceCatalogClient
from rss2discord.providers.woocommerce.models import (
    WooCommerceDiscoveryProduct,
    WooCommerceProduct,
    first_party_image_url,
)
from rss2discord.transports.base import ScraperStrategy


@final
class WooCommerceStrategy(ScraperStrategy):
    """Discover newly published products from any public WooCommerce shop."""

    seed_existing_on_first_fetch: bool = True
    require_entries_for_initialization: bool = True

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(
        self,
        url: str,
    ) -> tuple[list[WooCommerceDiscoveryProduct], str]:
        products = WooCommerceCatalogClient().fetch_latest_products(
            url,
            self._is_shutdown_requested,
        )
        # A zero price is an "ask for price" placeholder, not a product card.
        return (
            [
                product
                for product in products
                if product.product.prices.current_amount > 0
            ],
            WOOCOMMERCE_LABEL,
        )

    @override
    def get_entry_id(self, entry: WooCommerceDiscoveryProduct) -> EntryId:
        return EntryId(str(entry.product.id))

    @override
    def get_entry_data(self, entry: WooCommerceDiscoveryProduct) -> EntryData:
        product = entry.product
        return EntryData(
            title=product.name,
            link=product.permalink,
            description="",
            author="",
            timestamp=entry.published_at.isoformat(),
            image_url=first_party_image_url(product, entry.hostname),
            categories=tuple(category.name for category in product.categories),
            source_metrics=woocommerce_product_metrics(product),
            discount_percentage=discount_percentage(
                product.prices.regular_amount,
                product.prices.current_amount,
            ),
        )


def woocommerce_product_metrics(
    product: WooCommerceProduct,
    *,
    previous_price: str | None = None,
) -> tuple[SourceMetric, ...]:
    prices = product.prices
    metrics = [
        SourceMetric(
            "Price",
            format_woocommerce_price(prices.current_amount, prices.currency_code),
        ),
    ]
    if previous_price is not None:
        metrics.append(SourceMetric("Previous", previous_price, prior=True))
    if prices.regular_amount != prices.current_amount:
        metrics.append(
            SourceMetric(
                "Original",
                format_woocommerce_price(
                    prices.regular_amount,
                    prices.currency_code,
                ),
            ),
        )
    metrics.append(
        SourceMetric("Stock", "In stock" if product.is_in_stock else "Out of stock"),
    )
    if product.sku:
        metrics.append(SourceMetric("SKU", product.sku))
    return tuple(metrics)


def format_woocommerce_price(amount: Decimal, currency_code: str) -> str:
    canonical = canonicalize_price_amount(amount)
    whole_digits, _, fractional_digits = canonical.partition(".")
    grouped = f"{int(whole_digits):,}".replace(",", ".")
    significant_fraction = fractional_digits.rstrip("0")
    number = f"{grouped},{significant_fraction}" if significant_fraction else grouped
    suffix = {"MKD": " ден.", "EUR": " €"}.get(currency_code, f" {currency_code}")
    return f"{number}{suffix}"
