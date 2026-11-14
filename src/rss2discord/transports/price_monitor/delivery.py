"""Delivery stage for source-neutral price monitoring."""

import time
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from typing import assert_never
from uuid import uuid4

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.message import WebhookMessage
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationDeliveryResult,
)
from rss2discord.recovery_models import PriceSnapshot

from . import digest
from .contracts import (
    DeliverablePriceChange,
    PriceAlertDelivery,
    PriceChangeDeliveryDependencies,
    PriceDeliveryOutcome,
    PriceDeliveryPlan,
    PriceRecoveryStore,
)
from .planning import finish_price_delivery, record_price_health


def persist_price_delivery(store: PriceRecoveryStore, snapshot: PriceSnapshot) -> None:
    store.upsert_price_snapshot(snapshot)


def deliver_price_changes[PriceChangeT: DeliverablePriceChange](
    changes: Iterable[PriceChangeT],
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
    *,
    plan: PriceDeliveryPlan,
    feed: FeedConfig | None = None,
    source_title: str | None = None,
    baseline_extra: Sequence[PriceSnapshot] = (),
    current_snapshots: Sequence[PriceSnapshot] | None = None,
) -> PriceDeliveryOutcome:
    del source_title
    materialized = tuple(changes)
    if plan.bulk:
        return _deliver_bulk(
            materialized,
            dependencies,
            message_for,
            plan=plan,
            feed=feed,
            baseline_extra=baseline_extra,
            current_snapshots=current_snapshots,
        )
    if feed is not None:
        drained = drain_pending_price_notifications(dependencies, feed)
        if drained is PriceDeliveryOutcome.FAILED:
            return PriceDeliveryOutcome.FAILED
        if drained is PriceDeliveryOutcome.INTERRUPTED:
            return PriceDeliveryOutcome.INTERRUPTED
    selected = set(plan.selected_ids)
    delay_before_next = False
    had_failure = False
    for change in materialized:
        if change.current.product_id not in selected:
            continue
        if dependencies.delivery.is_shutdown_requested():
            return PriceDeliveryOutcome.INTERRUPTED
        if not _wait_between_price_deliveries(dependencies.delivery, delay_before_next):
            return PriceDeliveryOutcome.INTERRUPTED
        delay_before_next = False
        if dependencies.delivery.is_shutdown_requested():
            return PriceDeliveryOutcome.INTERRUPTED
        message = message_for(change)
        if dependencies.delivery.is_shutdown_requested():
            return PriceDeliveryOutcome.INTERRUPTED
        result = dependencies.sender.send(message, dependencies.delivery.sleep)
        if result is DiscordDeliveryResult.DELIVERED:
            dependencies.sqlite_retry_policy.execute(
                partial(
                    persist_price_delivery,
                    dependencies.snapshots,
                    change.current,
                ),
            )
            delay_before_next = True
            continue
        if result is DiscordDeliveryResult.INTERRUPTED:
            return PriceDeliveryOutcome.INTERRUPTED
        had_failure = True
        if dependencies.delivery.is_shutdown_requested():
            return PriceDeliveryOutcome.INTERRUPTED
    return (
        PriceDeliveryOutcome.FAILED if had_failure else PriceDeliveryOutcome.DELIVERED
    )


def _deliver_bulk[PriceChangeT: DeliverablePriceChange](
    changes: Sequence[PriceChangeT],
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
    *,
    plan: PriceDeliveryPlan,
    feed: FeedConfig | None,
    baseline_extra: Sequence[PriceSnapshot],
    current_snapshots: Sequence[PriceSnapshot] | None,
) -> PriceDeliveryOutcome:
    if feed is None:
        raise ValueError("bulk price delivery requires explicit feed context")
    messages = tuple((change, message_for(change)) for change in changes)
    notifications = tuple(
        PriceNotification(
            product_id=change.current.product_id,
            previous=change.previous,
            current=change.current,
            entry=message.entry,
            source_title=message.source_title,
        )
        for change, message in messages
    )
    observed_at = int(time.time())
    changed_ids = {notification.product_id for notification in notifications}
    baseline_changed = (
        tuple(
            snapshot
            for snapshot in current_snapshots
            if snapshot.product_id in changed_ids
        )
        if current_snapshots is not None
        else tuple(notification.current for notification in notifications)
    )
    count = dependencies.sqlite_retry_policy.execute(
        partial(
            dependencies.snapshots.record_price_scan,
            feed_id=feed.id,
            group_id=uuid4().hex,
            observed_at=observed_at,
            baseline_updates=(*baseline_changed, *baseline_extra),
            notifications=notifications,
        ),
    )
    result = digest.deliver_price_notifications(
        dependencies,
        feed,
        feed.id,
        additional_pending=plan.additional_pending,
    )
    match result:
        case PriceNotificationDeliveryResult.DRAINED:
            finish_price_delivery(
                dependencies.snapshots,
                feed.id,
                plan,
                plan.item_count or count,
            )
            return PriceDeliveryOutcome.DELIVERED
        case PriceNotificationDeliveryResult.FAILED:
            pending = dependencies.sqlite_retry_policy.execute(
                partial(
                    dependencies.snapshots.count_pending_price_notifications,
                    feed.id,
                ),
            )
            record_price_health(
                dependencies.snapshots,
                feed.id,
                "failed",
                "DeliveryRetryPending",
                pending,
            )
            return PriceDeliveryOutcome.FAILED
        case PriceNotificationDeliveryResult.INTERRUPTED:
            return PriceDeliveryOutcome.INTERRUPTED
        case unreachable:
            assert_never(unreachable)


def drain_pending_price_notifications(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
) -> PriceDeliveryOutcome:
    """Drain queued outbox groups and report the delivery outcome."""
    result = digest.deliver_price_notifications(dependencies, feed, feed.id)
    if result is PriceNotificationDeliveryResult.FAILED:
        pending = dependencies.sqlite_retry_policy.execute(
            partial(
                dependencies.snapshots.count_pending_price_notifications,
                feed.id,
            ),
        )
        record_price_health(
            dependencies.snapshots,
            feed.id,
            "failed",
            "DeliveryRetryPending",
            pending,
        )
        return PriceDeliveryOutcome.FAILED
    if result is PriceNotificationDeliveryResult.DRAINED:
        return PriceDeliveryOutcome.DELIVERED
    return PriceDeliveryOutcome.INTERRUPTED


def _wait_between_price_deliveries(
    delivery: PriceAlertDelivery,
    delay_before_next: bool,
) -> bool:
    if delay_before_next and delivery.delay_between_posts > 0:
        return delivery.sleep(delivery.delay_between_posts)
    return True
