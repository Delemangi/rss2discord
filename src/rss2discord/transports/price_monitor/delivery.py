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
) -> None:
    del source_title
    materialized = tuple(changes)
    if plan.bulk:
        _deliver_bulk(
            materialized,
            dependencies,
            message_for,
            plan=plan,
            feed=feed,
            baseline_extra=baseline_extra,
            current_snapshots=current_snapshots,
        )
        return
    if feed is not None and not _drain_pending(dependencies, feed):
        return
    selected = set(plan.selected_ids)
    delay_before_next = False
    for change in materialized:
        if change.current.product_id not in selected:
            continue
        if dependencies.delivery.is_shutdown_requested():
            return
        if not _wait_between_price_deliveries(dependencies.delivery, delay_before_next):
            return
        delay_before_next = False
        if dependencies.delivery.is_shutdown_requested():
            return
        message = message_for(change)
        if dependencies.delivery.is_shutdown_requested():
            return
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
            return
        if dependencies.delivery.is_shutdown_requested():
            return


def _deliver_bulk[PriceChangeT: DeliverablePriceChange](
    changes: Sequence[PriceChangeT],
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
    *,
    plan: PriceDeliveryPlan,
    feed: FeedConfig | None,
    baseline_extra: Sequence[PriceSnapshot],
    current_snapshots: Sequence[PriceSnapshot] | None,
) -> None:
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
    result = digest.deliver_price_notifications(dependencies, feed, feed.id)
    match result:
        case PriceNotificationDeliveryResult.DRAINED:
            finish_price_delivery(dependencies.snapshots, feed.id, plan, count)
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
        case PriceNotificationDeliveryResult.INTERRUPTED:
            return
        case unreachable:
            assert_never(unreachable)


def drain_pending_price_notifications(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
) -> bool:
    """Drain queued outbox groups, returning whether the feed is drained."""
    return _drain_pending(dependencies, feed)


def _drain_pending(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
) -> bool:
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
        return False
    return result is PriceNotificationDeliveryResult.DRAINED


def _wait_between_price_deliveries(
    delivery: PriceAlertDelivery,
    delay_before_next: bool,
) -> bool:
    if delay_before_next and delivery.delay_between_posts > 0:
        return delivery.sleep(delivery.delay_between_posts)
    return True
