"""DDStore public GraphQL catalog strategy."""

from collections.abc import Callable
from decimal import Decimal
from typing import Final, assert_never, final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.ddstore_catalog import DDStoreCatalogClient
from rss2discord.transports.ddstore_http import DDSTORE_LABEL
from rss2discord.transports.ddstore_models import DDStoreProduct, DDStoreStockStatus

DDSTORE_UNAVAILABLE_PRICE_LABEL: Final = "Ask for price"
MAX_DDSTORE_DELIVERY_HISTORY: Final = 50_000


@final
class DDStoreStrategy(ScraperStrategy):
    """Discover the latest DDStore products from its public GraphQL catalog."""

    seed_existing_on_first_fetch = True
    max_delivery_history = MAX_DDSTORE_DELIVERY_HISTORY

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[DDStoreProduct], str]:
        """Fetch the sorted latest DDStore product window."""
        products = DDStoreCatalogClient().fetch_latest_products(
            url,
            is_shutdown_requested=self._is_shutdown_requested,
        )
        return list(products), DDSTORE_LABEL

    @override
    def get_entry_id(self, entry: DDStoreProduct) -> EntryId:
        """Return the stable DDStore GraphQL UID."""
        return EntryId(entry.uid)

    @override
    def get_entry_data(self, entry: DDStoreProduct) -> EntryData:
        """Map a validated DDStore catalog product to Discord entry data."""
        minimum_price = entry.price_range.minimum_price
        final_price = minimum_price.final_price
        metrics = [
            SourceMetric(
                label="Price",
                value=(
                    format_ddstore_mkd(final_price.value)
                    if is_ddstore_price_available(final_price.value)
                    else DDSTORE_UNAVAILABLE_PRICE_LABEL
                ),
            ),
        ]
        regular_price = minimum_price.regular_price
        if (
            is_ddstore_price_available(final_price.value)
            and regular_price is not None
            and regular_price.value != final_price.value
        ):
            metrics.append(
                SourceMetric(
                    label="Original",
                    value=format_ddstore_mkd(regular_price.value),
                ),
            )
        metrics.append(
            SourceMetric(
                label="Stock",
                value=format_ddstore_stock(entry.stock_status),
            ),
        )
        return EntryData(
            title=entry.name,
            link=entry.product_url,
            description="",
            author="",
            timestamp=entry.created_at.isoformat(),
            image_url=entry.small_image.url if entry.small_image is not None else None,
            categories=tuple(
                category.name
                for category in entry.categories or ()
                if category.name is not None
            ),
            source_metrics=tuple(metrics),
        )


def is_ddstore_price_available(amount: Decimal) -> bool:
    return amount > 0


def format_ddstore_mkd(amount: int | Decimal) -> str:
    """Format MKD with dot-grouped whole digits and comma fractional digits."""
    canonical_amount = canonicalize_price_amount(Decimal(amount))
    whole_digits, _, fractional_digits = canonical_amount.partition(".")
    grouped_whole_digits = f"{int(whole_digits):,}".replace(",", ".")
    significant_fractional_digits = fractional_digits.rstrip("0")
    if significant_fractional_digits:
        return f"{grouped_whole_digits},{significant_fractional_digits} ден."
    return f"{grouped_whole_digits} ден."


def format_ddstore_stock(stock_status: DDStoreStockStatus) -> str:
    """Map DDStore stock statuses to stable user-facing text."""
    match stock_status:
        case "IN_STOCK":
            return "In stock"
        case "OUT_OF_STOCK":
            return "Out of stock"
        case unreachable:
            assert_never(unreachable)
