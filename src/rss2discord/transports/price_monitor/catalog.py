"""Catalog stage for source-neutral price monitoring."""

from collections.abc import Callable, Mapping
from functools import partial

from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.recovery_models import PriceChangeRecord, PriceSnapshot
from rss2discord.retries import FeedFetchInterruptedError

from .comparison import diff_price_snapshots
from .contracts import CatalogPriceChange, PriceChangeDeliveryDependencies
from .delivery import deliver_planned_price_changes
from .planning import prepare_price_delivery


def deliver_catalog_price_changes[ChangeT: CatalogPriceChange](
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[ChangeT], WebhookMessage],
    *,
    change_for: Callable[[PriceSnapshot, PriceSnapshot], ChangeT],
    feed_id: str,
    provider: str,
    current: Mapping[str, PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
    catalog_count: int,
    availability_returns: Mapping[str, tuple[PriceSnapshot, str]] | None = None,
    operation_started_at: float,
) -> None:
    """Compare complete catalog snapshots and deliver without extra confirmation I/O."""
    restoration_baseline = persisted
    held = dependencies.snapshots.held_price_product_ids(feed_id)
    current = {key: value for key, value in current.items() if key not in held}
    persisted = {key: value for key, value in persisted.items() if key not in held}
    silent, records = diff_price_snapshots(current.values(), persisted)
    changes = [change_for(record.previous, record.current) for record in records]
    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError
    store = dependencies.snapshots
    plan = dependencies.sqlite_retry_policy.execute(
        lambda: prepare_price_delivery(
            store=store,
            feed_id=feed_id,
            provider=provider,
            changes=tuple(
                PriceChangeRecord(c.current.product_id, c.previous, c.current)
                for c in changes
            ),
            current=current,
            persisted=persisted,
            catalog_count=catalog_count,
        ),
    )
    if plan.blocked:
        return
    if (
        availability_returns
        and plan.allow_silent_updates
        and not _restore_availability_returns(
            dependencies,
            feed_id,
            provider,
            availability_returns,
            restoration_baseline,
            operation_started_at=operation_started_at,
        )
    ):
        return
    deliver_planned_price_changes(
        dependencies,
        message_for,
        feed_id=feed_id,
        plan=plan,
        silent_updates=silent,
        changes=changes,
        item_count=len(current),
    )


def prepare_price_scan[ProductT](
    *,
    fetch_products: Callable[[], tuple[ProductT, ...]],
    load_snapshots: Callable[[], tuple[PriceSnapshot, ...]],
    is_shutdown_requested: Callable[[], bool],
    snapshot_limit: int,
    label: str,
) -> tuple[tuple[ProductT, ...], tuple[PriceSnapshot, ...]]:
    """Run the common shutdown, catalog, and snapshot scan preamble."""
    if is_shutdown_requested():
        raise FeedFetchInterruptedError
    products = fetch_products()
    if is_shutdown_requested():
        raise FeedFetchInterruptedError
    persisted = load_snapshots()
    if len(persisted) > snapshot_limit:
        raise FeedFetchError(label, "SnapshotLimitExceeded")
    return products, persisted


def _restore_availability_returns(
    dependencies: PriceChangeDeliveryDependencies,
    feed_id: str,
    provider: str,
    availability_returns: Mapping[str, tuple[PriceSnapshot, str]],
    restoration_baseline: Mapping[str, PriceSnapshot],
    *,
    operation_started_at: float,
) -> bool:
    origins_query = getattr(
        dependencies.snapshots,
        "availability_hold_origins",
        None,
    )
    restore = getattr(dependencies.snapshots, "restore_availability_hold", None)
    if origins_query is not None and restore is not None:
        origins = {
            str(row["product_id"]): str(row["reconciliation_fingerprint"])
            for row in origins_query(feed_id)
        }
        for product_id, (observed, context) in availability_returns.items():
            origin = origins.get(product_id)
            previous = restoration_baseline.get(product_id)
            if origin is None:
                continue
            if dependencies.delivery.is_shutdown_requested():
                return False
            dependencies.sqlite_retry_policy.execute(
                partial(
                    restore,
                    feed_id=feed_id,
                    provider=provider,
                    product_id=product_id,
                    origin_fingerprint=origin,
                    previous=previous,
                    observed=observed,
                    context=context,
                    source="validated_full_catalog",
                    operation_started_at=operation_started_at,
                ),
            )
    return True
