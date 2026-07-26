"""Setec public catalog strategy."""

from decimal import Decimal
from typing import Final, final, override
from urllib.parse import urljoin

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.setec_catalog import SetecCatalogClient
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_LATEST_RESPONSE_BYTES as MAX_SETEC_RESPONSE_BYTES,
)
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_REDIRECTS,
    SETEC_API_PATH,
    SETEC_LABEL,
    SETEC_REGION_ID,
    SETEC_STREAM_CHUNK_BYTES,
    SETEC_USER_AGENT,
    SETEC_WINDOW_SIZE,
)
from rss2discord.transports.setec_models import SetecProduct

SETEC_PRODUCT_BASE_URL: Final = "https://setec.mk/products/"

__all__ = [
    "MAX_SETEC_REDIRECTS",
    "MAX_SETEC_RESPONSE_BYTES",
    "SETEC_API_PATH",
    "SETEC_LABEL",
    "SETEC_PRODUCT_BASE_URL",
    "SETEC_REGION_ID",
    "SETEC_STREAM_CHUNK_BYTES",
    "SETEC_USER_AGENT",
    "SETEC_WINDOW_SIZE",
    "SetecProduct",
    "SetecStrategy",
    "format_setec_mkd",
]


@final
class SetecStrategy(ScraperStrategy):
    """Discover newly listed products from the public Setec catalog."""

    seed_existing_on_first_fetch: bool = True

    @override
    def fetch_entries(self, url: str) -> tuple[list[SetecProduct], str]:
        """Fetch the latest window of Setec products via the medusa API."""
        products = SetecCatalogClient().fetch_latest_products(url)
        return list(products), SETEC_LABEL

    @override
    def get_entry_id(self, entry: SetecProduct) -> EntryId:
        """Return the stable Setec product ID."""
        return EntryId(entry.id)

    @override
    def get_entry_data(self, entry: SetecProduct) -> EntryData:
        """Map a catalog product to Discord entry data."""
        price_variant = entry.variants[0].calculated_price if entry.variants else None
        metrics: list[SourceMetric] = []
        if price_variant is not None:
            metrics.append(
                SourceMetric(
                    label="Price",
                    value=format_setec_mkd(price_variant.calculated_amount),
                ),
            )
            if price_variant.original_amount != price_variant.calculated_amount:
                metrics.append(
                    SourceMetric(
                        label="Original",
                        value=format_setec_mkd(price_variant.original_amount),
                    ),
                )
        return EntryData(
            title=entry.title,
            link=urljoin(SETEC_PRODUCT_BASE_URL, entry.handle),
            description="",
            author="",
            timestamp=entry.created_at.isoformat(),
            image_url=entry.thumbnail or None,
            categories=tuple(category.name for category in entry.categories),
            source_metrics=tuple(metrics),
        )


def format_setec_mkd(amount: int | Decimal) -> str:
    """Format MKD with dot-grouped whole digits and comma fractional digits."""
    canonical_amount = canonicalize_price_amount(Decimal(amount))
    whole_digits, _, fractional_digits = canonical_amount.partition(".")
    grouped_whole_digits = f"{int(whole_digits):,}".replace(",", ".")
    significant_fractional_digits = fractional_digits.rstrip("0")
    if significant_fractional_digits:
        return f"{grouped_whole_digits},{significant_fractional_digits} ден."
    return f"{grouped_whole_digits} ден."
