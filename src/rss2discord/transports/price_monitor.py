"""Source-neutral contracts for sequential catalog price monitors."""

import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from itertools import islice
from typing import Protocol, assert_never

from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    DiscordSender,
    SleepCallback,
)
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import PriceDirection
from rss2discord.price_safety import (
    HEALTH_REMINDER_SECONDS,
    MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
    MAX_UNAPPROVED_PRICE_CHANGES,
    assess_price_safety,
    canonical_manifest_fingerprint,
)
from rss2discord.recovery_models import (
    HealthNotice,
    HealthUpdate,
    PriceBatch,
    PriceBatchItem,
    PriceChangeRecord,
)
from rss2discord.retries import FeedFetchInterruptedError, SQLiteRetryPolicy

logger = logging.getLogger(__name__)


def price_direction(
    previous: PriceSnapshot,
    current: PriceSnapshot,
) -> PriceDirection | None:
    """Report which way a monitored price moved.

    Returns ``None`` when the two amounts are not comparable, such as a change
    that crossed currencies, so callers never claim a direction the snapshots
    cannot establish.
    """
    if previous.currency != current.currency:
        return None
    if current.amount < previous.amount:
        return PriceDirection.DECREASE
    return PriceDirection.INCREASE


class PriceSnapshotStore(Protocol):
    """Persist source-neutral price snapshots for one feed."""

    def load_price_snapshots(self, feed_id: str) -> tuple[PriceSnapshot, ...]: ...

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None: ...

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None: ...


class PriceRecoveryStore(PriceSnapshotStore, Protocol):
    def record_price_change_candidate(
        self,
        *,
        feed_id: str,
        provider: str,
        fingerprint: str,
        catalog_count: int,
        available_count: int,
        items: Iterable[PriceChangeRecord],
    ) -> PriceBatch: ...

    def load_active_price_batch(self, feed_id: str) -> PriceBatch | None: ...

    def pause_price_change_batch(self, batch_id: int, reason: str) -> PriceBatch: ...

    def begin_price_delivery_attempt(
        self,
        batch_id: int,
        *,
        max_attempts: int = MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
    ) -> PriceBatchItem | None: ...

    def record_approved_price_delivery(
        self,
        batch_id: int,
        product_id: str,
        snapshot: PriceSnapshot,
    ) -> None: ...

    def complete_price_change_batch_if_drained(self, batch_id: int) -> bool: ...

    def record_health(
        self,
        update: HealthUpdate,
        *,
        reminder_seconds: int = HEALTH_REMINDER_SECONDS,
    ) -> HealthNotice: ...


@dataclass(frozen=True, slots=True)
class PriceDeliveryPlan:
    selected_ids: tuple[str, ...] = ()
    batch_id: int | None = None
    blocked: bool = False

    @property
    def allow_silent_updates(self) -> bool:
        return not self.blocked and self.batch_id is None


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
    batch = store.load_active_price_batch(feed_id)
    if batch is None:
        return False
    if batch.status == "approved":
        store.pause_price_change_batch(batch.batch_id, cause)
    record_price_health(store, feed_id, "recovery_required", cause, batch.pending_count)
    return True


def pause_price_fetch_failure(
    store: PriceRecoveryStore,
    feed_id: str,
    error: FeedFetchError,
) -> bool:
    paused = pause_price_recovery(store, feed_id, error.cause_type)
    if paused and (
        error.cause_type in {"AccessChallenge", "BotChallenge"}
        or error.status_code == 403
    ):
        record_price_health(store, feed_id, "blocked", error.cause_type, 0)
    return paused


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
    """Gate a complete diff before writes/resolution, then reserve bounded work."""
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
        pending = tuple(item for item in batch.items if item.status == "pending")
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
        selected: list[str] = []
        # Reserve only the current least-attempted tier. Advancing a claim
        # removes it from this tier, so a batch cannot select an ID twice.
        # The store's ceiling is cumulative, not a lifetime retry policy.
        minimum_attempts = min(item.attempt_count for item in pending)
        tier_count = sum(item.attempt_count == minimum_attempts for item in pending)
        for _ in range(min(tier_count, MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN)):
            item = store.begin_price_delivery_attempt(
                batch.batch_id,
                max_attempts=minimum_attempts + 1,
            )
            if item is None or item.product_id in selected:
                pause_price_recovery(store, feed_id, "PriceAttemptSelectionUnavailable")
                return PriceDeliveryPlan(batch_id=batch.batch_id, blocked=True)
            selected.append(item.product_id)
        return PriceDeliveryPlan(tuple(selected), batch.batch_id)
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
    return PriceDeliveryPlan(
        tuple(
            change.product_id
            for change in changes[:MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN]
        ),
    )


def persist_price_delivery(
    store: PriceRecoveryStore,
    plan: PriceDeliveryPlan,
    snapshot: PriceSnapshot,
) -> None:
    if plan.batch_id is None:
        store.upsert_price_snapshot(snapshot)
    else:
        store.record_approved_price_delivery(
            plan.batch_id,
            snapshot.product_id,
            snapshot,
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


@dataclass(frozen=True, slots=True)
class PriceAlertDelivery:
    """Control sequential Discord delivery and observe runtime shutdown state."""

    sleep: SleepCallback
    delay_between_posts: float
    is_shutdown_requested: Callable[[], bool]


class DeliverablePriceChange(Protocol):
    @property
    def current(self) -> PriceSnapshot: ...


class PriceChangeDeliveryDependencies(Protocol):
    @property
    def snapshots(self) -> PriceSnapshotStore: ...

    @property
    def sender(self) -> DiscordSender: ...

    @property
    def sqlite_retry_policy(self) -> SQLiteRetryPolicy: ...

    @property
    def delivery(self) -> PriceAlertDelivery: ...


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


def deliver_price_changes[PriceChangeT: DeliverablePriceChange](
    changes: Iterable[PriceChangeT],
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
    *,
    on_delivered: Callable[[PriceSnapshot], None] | None = None,
) -> None:
    delay_before_next = False
    for change in islice(changes, MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN):
        if dependencies.delivery.is_shutdown_requested():
            return
        if (
            delay_before_next
            and dependencies.delivery.delay_between_posts > 0
            and not dependencies.delivery.sleep(
                dependencies.delivery.delay_between_posts,
            )
        ):
            return
        delay_before_next = False
        if dependencies.delivery.is_shutdown_requested():
            return
        result = dependencies.sender.send(
            message_for(change),
            dependencies.delivery.sleep,
        )
        match result:
            case DiscordDeliveryResult.DELIVERED:
                dependencies.sqlite_retry_policy.execute(
                    partial(
                        on_delivered or dependencies.snapshots.upsert_price_snapshot,
                        change.current,
                    ),
                )
                delay_before_next = True
            case DiscordDeliveryResult.FAILED:
                if dependencies.delivery.is_shutdown_requested():
                    return
            case DiscordDeliveryResult.INTERRUPTED:
                return
            case unreachable:
                assert_never(unreachable)
