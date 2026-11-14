"""Planning stage for source-neutral price monitoring."""

import logging
import time
from collections.abc import Mapping, Sequence

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import MAX_INLINE_PRICE_CHANGES
from rss2discord.recovery_models import (
    HealthUpdate,
    PriceChangeRecord,
    PriceSnapshot,
)

from .contracts import PriceDeliveryPlan, PriceRecoveryStore

logger = logging.getLogger("rss2discord.transports.price_monitor")


def record_price_health(
    store: PriceRecoveryStore,
    feed_id: str,
    state: str,
    cause: str | None,
    item_count: int,
) -> None:
    notice = store.record_health(
        HealthUpdate(
            feed_id=feed_id,
            job_kind="price",
            state=state,
            cause=cause,
            attempted_at=int(time.time()),
            success=state in {"healthy", "empty"},
            nonempty=item_count > 0,
            item_count=item_count,
        ),
    )
    if notice.should_log:
        logger.info("Price health for feed %s: %s (%s)", feed_id, state, cause)


def validate_price_currencies(
    *,
    provider: str,
    current: Mapping[str, PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
) -> None:
    if any(
        snapshot.currency != persisted[product_id].currency
        for product_id, snapshot in current.items()
        if product_id in persisted
    ):
        raise FeedFetchError(provider, "CurrencyChanged")


def prepare_price_delivery(
    *,
    store: PriceRecoveryStore,
    feed_id: str,
    provider: str,
    changes: Sequence[PriceChangeRecord],
    current: Mapping[str, PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
    catalog_count: int,
    force_summary: bool = False,
    additional_pending: int = 0,
) -> PriceDeliveryPlan:
    """Validate currency stability and choose inline cards or one digest."""
    del store, feed_id
    validate_price_currencies(
        provider=provider,
        current=current,
        persisted=persisted,
    )
    if force_summary or len(changes) > MAX_INLINE_PRICE_CHANGES:
        return PriceDeliveryPlan(
            bulk=True,
            additional_pending=additional_pending,
            item_count=catalog_count,
        )
    return PriceDeliveryPlan(
        tuple(change.product_id for change in changes),
        item_count=catalog_count,
    )


def finish_price_delivery(
    store: PriceRecoveryStore,
    feed_id: str,
    plan: PriceDeliveryPlan,
    item_count: int,
) -> None:
    """Record health only; pending outbox work remains a failure."""
    del plan
    pending = store.count_pending_price_notifications(feed_id)
    if pending:
        record_price_health(
            store,
            feed_id,
            "failed",
            "DeliveryRetryPending",
            pending,
        )
        return
    record_price_health(
        store,
        feed_id,
        "healthy" if item_count else "empty",
        None,
        item_count,
    )
