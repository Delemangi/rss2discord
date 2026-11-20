"""Delivery stage for source-neutral price monitoring."""

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from functools import partial
from typing import assert_never
from uuid import uuid4

from rss2discord.configuration import FeedConfig
from rss2discord.defaults import PRICE_HISTORY_WINDOW_DAYS
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceHistoryContext, PriceSnapshot
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationDeliveryResult,
    PriceNotificationRecord,
)

from .contracts import (
    DeliverablePriceChange,
    PriceAlertDelivery,
    PriceChangeDeliveryDependencies,
    PriceDeliveryOutcome,
    PriceDeliveryPlan,
)
from .digest import build_digest_messages
from .planning import finish_price_delivery, record_price_health


def deliver_price_changes[PriceChangeT: DeliverablePriceChange](
    changes: Iterable[PriceChangeT],
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
    *,
    plan: PriceDeliveryPlan,
    feed: FeedConfig | None = None,
    baseline_extra: Sequence[PriceSnapshot] = (),
) -> PriceDeliveryOutcome:
    materialized = tuple(changes)
    if plan.bulk:
        return _deliver_bulk(
            materialized,
            dependencies,
            message_for,
            plan=plan,
            feed=feed,
            baseline_extra=baseline_extra,
        )
    if feed is not None:
        drained = drain_pending_price_notifications(dependencies, feed)
        if drained is PriceDeliveryOutcome.FAILED:
            return PriceDeliveryOutcome.FAILED
        if drained is PriceDeliveryOutcome.INTERRUPTED:
            return PriceDeliveryOutcome.INTERRUPTED
    selected = set(plan.selected_ids)
    selected_changes = tuple(
        change for change in materialized if change.current.product_id in selected
    )
    history_contexts: Mapping[tuple[str, str], PriceHistoryContext] | None = None
    delay_before_next = False
    had_failure = False
    for change in selected_changes:
        if dependencies.delivery.is_shutdown_requested():
            return PriceDeliveryOutcome.INTERRUPTED
        if not _wait_between_price_deliveries(dependencies.delivery, delay_before_next):
            return PriceDeliveryOutcome.INTERRUPTED
        delay_before_next = False
        if dependencies.delivery.is_shutdown_requested():
            return PriceDeliveryOutcome.INTERRUPTED
        message = message_for(change)
        if message.entry.price_change_percentage is not None or message.entry.restocked:
            if history_contexts is None:
                history_contexts = _load_price_history_contexts(
                    dependencies,
                    (candidate.current for candidate in selected_changes),
                )
            message = _with_price_history(
                message,
                history_contexts.get(
                    (change.current.feed_id, change.current.product_id),
                ),
            )
        if dependencies.delivery.is_shutdown_requested():
            return PriceDeliveryOutcome.INTERRUPTED
        result = dependencies.sender.send(message, dependencies.delivery.sleep)
        if result is DiscordDeliveryResult.DELIVERED:
            dependencies.sqlite_retry_policy.execute(
                partial(
                    dependencies.snapshots.upsert_price_snapshot,
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
    baseline_changed = tuple(notification.current for notification in notifications)
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
    result = deliver_price_notifications(
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


def deliver_price_notifications(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    feed_id: str,
    *,
    additional_pending: int = 0,
) -> PriceNotificationDeliveryResult:
    """Drain every pending group oldest-first, one Discord message at a time."""
    while not dependencies.delivery.is_shutdown_requested():
        group = dependencies.sqlite_retry_policy.execute(
            partial(dependencies.snapshots.load_pending_price_group, feed_id),
        )
        if group is None:
            return PriceNotificationDeliveryResult.DRAINED
        _group_id, records = group
        # Choose by the pending group's size, not by the scan that recorded it:
        # rotated confirmation can record fewer rows than the scan found.
        result = (
            _deliver_inline_cards(dependencies, feed, records)
            if len(records) <= feed.price_inline_limit
            else _deliver_digest(
                dependencies,
                feed,
                records,
                additional_pending=additional_pending,
            )
        )
        if result is not PriceNotificationDeliveryResult.DRAINED:
            return result
    return PriceNotificationDeliveryResult.INTERRUPTED


def _deliver_inline_cards(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
) -> PriceNotificationDeliveryResult:
    contexts = _load_price_history_contexts(
        dependencies,
        (
            PriceSnapshot(
                feed_id=record.feed_id,
                product_id=record.product_id,
                amount=record.current_amount,
                formatted=record.current_price,
                currency=record.currency,
            )
            for record in records
            if record.entry.price_change_percentage is not None
            or record.entry.restocked
        ),
    )
    delay_before_next = False
    for index, record in enumerate(records):
        if dependencies.delivery.is_shutdown_requested():
            return PriceNotificationDeliveryResult.INTERRUPTED
        if not _wait_before_next(dependencies, delay_before_next):
            return PriceNotificationDeliveryResult.INTERRUPTED
        delay_before_next = False
        message = _with_price_history(
            WebhookMessage(
                feed=feed,
                entry=record.entry,
                source_title=record.source_title,
            ),
            contexts.get((record.feed_id, record.product_id)),
        )
        result = dependencies.sender.send(message, dependencies.delivery.sleep)
        if result is DiscordDeliveryResult.DELIVERED:
            _mark_sent(dependencies, (record.notification_id,))
            delay_before_next = True
            continue
        if result is DiscordDeliveryResult.INTERRUPTED:
            return PriceNotificationDeliveryResult.INTERRUPTED
        _record_attempt(dependencies, records[index:])
        return PriceNotificationDeliveryResult.FAILED
    return PriceNotificationDeliveryResult.DRAINED


def _deliver_digest(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    records: Sequence[PriceNotificationRecord],
    *,
    additional_pending: int = 0,
) -> PriceNotificationDeliveryResult:
    for index, message in enumerate(
        build_digest_messages(
            feed,
            records,
            additional_pending=additional_pending,
        ),
    ):
        if dependencies.delivery.is_shutdown_requested():
            return PriceNotificationDeliveryResult.INTERRUPTED
        if index and not _wait_before_next(dependencies, True):
            return PriceNotificationDeliveryResult.INTERRUPTED
        result = dependencies.sender.send(message, dependencies.delivery.sleep)
        if result is DiscordDeliveryResult.DELIVERED:
            continue
        if result is DiscordDeliveryResult.INTERRUPTED:
            return PriceNotificationDeliveryResult.INTERRUPTED
        _record_attempt(dependencies, records)
        return PriceNotificationDeliveryResult.FAILED
    _mark_sent(dependencies, (record.notification_id for record in records))
    return PriceNotificationDeliveryResult.DRAINED


def _load_price_history_contexts(
    dependencies: PriceChangeDeliveryDependencies,
    observations: Iterable[PriceSnapshot],
) -> Mapping[tuple[str, str], PriceHistoryContext]:
    """Load one history context per product with a single call per feed."""
    grouped: dict[str, dict[str, PriceSnapshot]] = {}
    for observation in observations:
        grouped.setdefault(observation.feed_id, {})[observation.product_id] = (
            observation
        )
    contexts: dict[tuple[str, str], PriceHistoryContext] = {}
    for feed_id, feed_observations in grouped.items():
        loaded = dependencies.sqlite_retry_policy.execute(
            partial(
                dependencies.snapshots.load_price_history_context,
                feed_id,
                feed_observations,
            ),
        )
        contexts.update(
            ((feed_id, product_id), context) for product_id, context in loaded.items()
        )
    return contexts


def _with_price_history(
    message: WebhookMessage,
    context: PriceHistoryContext | None,
) -> WebhookMessage:
    """Attach range context to an inline change or restock card when one is known."""
    if context is None:
        return message
    if message.entry.price_change_percentage is None and not message.entry.restocked:
        return message
    return replace(
        message,
        entry=replace(
            message.entry,
            price_period_low=context.low_formatted,
            price_period_high=context.high_formatted,
            price_period_days=PRICE_HISTORY_WINDOW_DAYS,
            price_is_period_low=context.is_low,
        ),
    )


def _wait_before_next(
    dependencies: PriceChangeDeliveryDependencies,
    delay_before_next: bool,
) -> bool:
    if not delay_before_next or dependencies.delivery.delay_between_posts <= 0:
        return True
    return dependencies.delivery.sleep(dependencies.delivery.delay_between_posts)


def _mark_sent(
    dependencies: PriceChangeDeliveryDependencies,
    notification_ids: Iterable[int],
) -> None:
    dependencies.sqlite_retry_policy.execute(
        partial(
            dependencies.snapshots.mark_price_notifications_sent,
            tuple(notification_ids),
        ),
    )


def _record_attempt(
    dependencies: PriceChangeDeliveryDependencies,
    records: Iterable[PriceNotificationRecord],
) -> None:
    ids = tuple(record.notification_id for record in records)
    dependencies.sqlite_retry_policy.execute(
        partial(
            dependencies.snapshots.record_price_notification_attempt,
            ids,
            "DiscordDeliveryFailed",
        ),
    )


def drain_pending_price_notifications(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
) -> PriceDeliveryOutcome:
    """Drain queued outbox groups and report the delivery outcome."""
    result = deliver_price_notifications(dependencies, feed, feed.id)
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
