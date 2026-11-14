"""Generic full-catalog observation normalization shared by price monitors."""

from dataclasses import dataclass

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import PriceSnapshot


@dataclass(frozen=True, slots=True)
class CatalogObservation:
    product_id: str
    snapshot: PriceSnapshot | None


def validate_catalog_observations(
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
