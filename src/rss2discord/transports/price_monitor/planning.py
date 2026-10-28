"""Planning stage for source-neutral price monitoring."""

import logging
import time
from collections.abc import Callable, Mapping, Sequence

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import (
    MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
    MAX_UNAPPROVED_PRICE_CHANGES,
    assess_price_safety,
    canonical_manifest_fingerprint,
)
from rss2discord.recovery_models import (
    HealthUpdate,
    PriceBatch,
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


def pause_price_recovery(store: PriceRecoveryStore, feed_id: str, cause: str) -> bool:
    batch = _pause_price_batch(store, feed_id, cause)
    if batch is None:
        return False
    record_price_health(store, feed_id, "recovery_required", cause, batch.pending_count)
    return True


def _pause_price_batch(
    store: PriceRecoveryStore,
    feed_id: str,
    cause: str,
) -> PriceBatch | None:
    batch = store.load_active_price_batch(feed_id)
    if batch is None:
        return None
    if batch.status == "approved":
        store.pause_price_change_batch(batch.batch_id, cause)
    return batch


def pause_price_fetch_failure(
    store: PriceRecoveryStore,
    feed_id: str,
    error: FeedFetchError,
) -> bool:
    batch = _pause_price_batch(store, feed_id, error.cause_type)
    if batch is None:
        return False
    blocked = (
        error.cause_type in {"AccessChallenge", "BotChallenge"}
        or error.status_code == 403
    )
    record_price_health(
        store,
        feed_id,
        "blocked" if blocked else "recovery_required",
        error.cause_type,
        0 if blocked else batch.pending_count,
    )
    return True


def run_price_scan(
    scan: Callable[[], None],
    store: PriceRecoveryStore,
    feed_id: str,
) -> None:
    """Pause an active recovery batch when the monitor fetch fails."""
    try:
        scan()
    except FeedFetchError as error:
        if not pause_price_fetch_failure(store, feed_id, error):
            raise


def prepare_price_delivery(
    *,
    store: PriceRecoveryStore,
    feed_id: str,
    provider: str,
    changes: Sequence[PriceChangeRecord],
    current: Mapping[str, PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
    catalog_count: int,
) -> PriceDeliveryPlan:
    """Validate the complete diff and select bounded work without claiming sends."""
    held = store.held_price_product_ids(feed_id)
    current = {key: value for key, value in current.items() if key not in held}
    persisted = {key: value for key, value in persisted.items() if key not in held}
    changes = tuple(change for change in changes if change.product_id not in held)
    batch = store.load_active_price_batch(feed_id)
    if batch is not None and batch.status == "paused":
        record_price_health(
            store,
            feed_id,
            "recovery_required",
            "PriceBatchPaused",
            batch.pending_count,
        )
        return PriceDeliveryPlan(batch_id=batch.batch_id, blocked=True)
    if any(
        snapshot.currency != persisted[product_id].currency
        for product_id, snapshot in current.items()
        if product_id in persisted
    ):
        if pause_price_recovery(store, feed_id, "CurrencyChanged"):
            return PriceDeliveryPlan(blocked=True)
        raise FeedFetchError(provider, "CurrencyChanged")
    if batch is not None:
        return _prepare_active_batch(
            store,
            feed_id,
            provider,
            batch,
            held,
            current=current,
            persisted=persisted,
        )
    if len(changes) > MAX_UNAPPROVED_PRICE_CHANGES:
        store.record_price_change_candidate(
            feed_id=feed_id,
            provider=provider,
            fingerprint=canonical_manifest_fingerprint(
                feed_id=feed_id,
                provider=provider,
                items=changes,
            ),
            catalog_count=catalog_count,
            available_count=len(current),
            items=changes,
        )
        record_price_health(
            store,
            feed_id,
            "quarantined",
            "PriceChangeLimitExceeded",
            len(changes),
        )
        return PriceDeliveryPlan(blocked=True)
    selected = store.select_normal_price_deliveries(
        feed_id=feed_id,
        product_ids=tuple(change.product_id for change in changes),
        limit=MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
    )
    return (
        PriceDeliveryPlan(blocked=True)
        if selected is None
        else PriceDeliveryPlan(selected)
    )


def finish_price_delivery(
    store: PriceRecoveryStore,
    feed_id: str,
    plan: PriceDeliveryPlan,
    item_count: int,
) -> None:
    if plan.batch_id is not None:
        store.complete_price_change_batch_if_drained(plan.batch_id)
    record_price_health(
        store,
        feed_id,
        "healthy" if item_count else "empty",
        None,
        item_count,
    )


def _prepare_active_batch(
    store: PriceRecoveryStore,
    feed_id: str,
    provider: str,
    batch: PriceBatch,
    held: frozenset[str],
    *,
    current: Mapping[str, PriceSnapshot],
    persisted: Mapping[str, PriceSnapshot],
) -> PriceDeliveryPlan:
    pending = tuple(
        item
        for item in batch.items
        if item.status == "pending" and item.product_id not in held
    )
    decision = assess_price_safety(
        expected=pending,
        current=current,
        persisted=persisted,
    )
    if batch.provider != provider or not decision.allowed:
        pause_price_recovery(store, feed_id, decision.reason or "ProviderChanged")
        return PriceDeliveryPlan(batch_id=batch.batch_id, blocked=True)
    if not pending:
        store.complete_price_change_batch_if_drained(batch.batch_id)
        return PriceDeliveryPlan(batch_id=batch.batch_id)
    ordered = sorted(
        pending,
        key=lambda item: (
            item.attempt_count,
            item.last_attempt_at if item.last_attempt_at is not None else -1,
            item.ordinal,
        ),
    )
    return PriceDeliveryPlan(
        tuple(
            item.product_id for item in ordered[:MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN]
        ),
        batch.batch_id,
    )
