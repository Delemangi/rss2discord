"""Pure full-catalog price normalization shared by monitors and reconciliation."""

from dataclasses import dataclass

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.providers.ddstore.models import DDStoreProduct
from rss2discord.providers.ddstore.strategy import (
    format_ddstore_mkd,
    is_ddstore_price_available,
)
from rss2discord.reconciliation_models import canonical_json, digest
from rss2discord.recovery_models import PriceSnapshot
from rss2discord.transports.hivetec import format_hivetec_mkd
from rss2discord.transports.hivetec_models import HivetecProduct

MAX_DDSTORE_RETAINED_SNAPSHOTS = 50_000
MAX_HIVETEC_RETAINED_SNAPSHOTS = 10_000


@dataclass(frozen=True, slots=True)
class CatalogObservation:
    product_id: str
    snapshot: PriceSnapshot | None
    context: str


def ddstore_snapshot(feed_id: str, product: DDStoreProduct) -> PriceSnapshot:
    price = product.price_range.minimum_price.final_price
    return PriceSnapshot(
        feed_id,
        product.uid,
        price.value,
        format_ddstore_mkd(price.value),
        price.currency,
    )


def hivetec_snapshot(feed_id: str, product: HivetecProduct) -> PriceSnapshot:
    price = product.prices
    return PriceSnapshot(
        feed_id,
        str(product.id),
        price.current_amount,
        format_hivetec_mkd(price.current_amount),
        price.currency_code,
    )


def normalize_ddstore_catalog(
    feed_id: str,
    products: tuple[DDStoreProduct, ...],
    persisted: tuple[PriceSnapshot, ...],
    *,
    snapshot_limit: int = MAX_DDSTORE_RETAINED_SNAPSHOTS,
) -> tuple[CatalogObservation, ...]:
    observations = tuple(
        CatalogObservation(
            product.uid,
            ddstore_snapshot(feed_id, product)
            if is_ddstore_price_available(
                product.price_range.minimum_price.final_price.value,
            )
            else None,
            _ddstore_context(product),
        )
        for product in products
    )
    _validate(observations, persisted, snapshot_limit, "DDStore")
    return observations


def normalize_hivetec_catalog(
    feed_id: str,
    products: tuple[HivetecProduct, ...],
    persisted: tuple[PriceSnapshot, ...],
    *,
    snapshot_limit: int = MAX_HIVETEC_RETAINED_SNAPSHOTS,
) -> tuple[CatalogObservation, ...]:
    observations = tuple(
        CatalogObservation(
            str(product.id),
            hivetec_snapshot(feed_id, product)
            if product.prices.current_amount > 0
            else None,
            _hivetec_context(product),
        )
        for product in products
    )
    _validate(observations, persisted, snapshot_limit, "Hivetec")
    return observations


def _ddstore_context(product: DDStoreProduct) -> str:
    minimum = product.price_range.minimum_price
    return canonical_json(
        {
            "name": product.name,
            "categories": [category.name for category in product.categories or ()],
            "created_at": product.created_at.isoformat(),
            "stock_status": product.stock_status,
            "final_price": canonicalize_price_amount(minimum.final_price.value),
            "currency": minimum.final_price.currency,
            "regular_price": canonicalize_price_amount(minimum.regular_price.value)
            if minimum.regular_price is not None
            else None,
            # Only the one-way digest retains omitted URL and other model context;
            # raw model serialization is never stored in an artifact or audit row.
            "source_context_digest": digest(product.model_dump(mode="json")),
        },
    )


def _hivetec_context(product: HivetecProduct) -> str:
    return canonical_json(
        {
            "name": product.name,
            "categories": [category.name for category in product.categories],
            "sku": product.sku,
            "is_in_stock": product.is_in_stock,
            "price": canonicalize_price_amount(product.prices.current_amount),
            "regular_price": canonicalize_price_amount(product.prices.regular_amount),
            "currency": product.prices.currency_code,
            "currency_minor_unit": product.prices.currency_minor_unit,
            "source_context_digest": digest(product.model_dump(mode="json")),
        },
    )


def _validate(
    observations: tuple[CatalogObservation, ...],
    persisted: tuple[PriceSnapshot, ...],
    limit: int,
    provider: str,
) -> None:
    if not observations:
        raise FeedFetchError(provider, "EmptyCatalog")
    ids = {item.product_id for item in observations}
    if len(ids) != len(observations):
        raise FeedFetchError(provider, "ConflictingProductIDs")
    retained = {snapshot.product_id for snapshot in persisted}
    available = {item.product_id for item in observations if item.snapshot is not None}
    if len(persisted) > limit or len(retained | available) > limit:
        raise FeedFetchError(provider, "SnapshotLimitExceeded")
