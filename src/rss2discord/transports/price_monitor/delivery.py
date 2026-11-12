"""Delivery stage for source-neutral price monitoring."""

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from functools import partial
from itertools import islice
from typing import assert_never

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData
from rss2discord.price_safety import MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN
from rss2discord.recovery_models import PriceDeliveryClaim, PriceSnapshot

from .contracts import (
    DeliverablePriceChange,
    PriceAlertDelivery,
    PriceChangeDeliveryDependencies,
    PriceDeliveryPlan,
    PriceRecoveryStore,
)
from .planning import finish_price_delivery, record_price_health


def persist_price_delivery(
    store: PriceRecoveryStore,
    claim: PriceDeliveryClaim | None,
    snapshot: PriceSnapshot,
) -> None:
    if claim is None:
        store.upsert_price_snapshot(snapshot)
    else:
        store.record_approved_price_delivery(claim, snapshot)


def deliver_price_changes[PriceChangeT: DeliverablePriceChange](
    changes: Iterable[PriceChangeT],
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
    *,
    plan: PriceDeliveryPlan,
    feed: FeedConfig | None = None,
    source_title: str | None = None,
) -> None:
    if plan.blocked:
        return
    if plan.summary is not None:
        if feed is None:
            raise ValueError("summary delivery requires explicit feed context")
        deliver_price_summary(
            dependencies,
            feed,
            source_title or feed.name or plan.summary.provider,
            plan,
        )
        return
    delay_before_next = False
    for change in islice(changes, MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN):
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
        claim = _claim_price_change(dependencies, plan, change.current.product_id)
        if plan.batch_id is not None and claim is None:
            return
        result = _send_claimed_price_change(dependencies, message, claim)
        should_continue, delay_before_next = _handle_price_delivery_result(
            dependencies,
            change.current,
            claim,
            result,
        )
        if not should_continue:
            return


def deliver_planned_price_changes[PriceChangeT: DeliverablePriceChange](
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
    *,
    feed_id: str,
    plan: PriceDeliveryPlan,
    silent_updates: Sequence[PriceSnapshot],
    changes: Sequence[PriceChangeT],
    item_count: int,
    feed: FeedConfig | None = None,
    source_title: str | None = None,
) -> None:
    """Apply permitted silent updates, deliver selected changes, and record health."""
    if plan.blocked:
        return
    if plan.summary is not None:
        if feed is None:
            raise ValueError("summary delivery requires explicit feed context")
        deliver_price_summary(
            dependencies,
            feed,
            source_title or feed.name or plan.summary.provider,
            plan,
        )
        return
    if silent_updates and plan.allow_silent_updates:
        dependencies.sqlite_retry_policy.execute(
            lambda: dependencies.snapshots.upsert_price_snapshots(silent_updates),
        )
    changes_by_id = {change.current.product_id: change for change in changes}
    deliver_price_changes(
        (changes_by_id[product_id] for product_id in plan.selected_ids),
        dependencies,
        message_for,
        plan=plan,
    )
    finish_price_delivery(
        dependencies.snapshots,
        feed_id,
        plan,
        item_count,
    )


def deliver_price_summary(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    source_title: str,
    plan: PriceDeliveryPlan,
) -> None:
    """Send once outside SQLite transactions; retry only the local acknowledgement."""
    batch = plan.summary
    if batch is None or dependencies.delivery.is_shutdown_requested():
        return
    claim = dependencies.sqlite_retry_policy.execute(
        partial(dependencies.snapshots.claim_price_summary_attempt, batch.batch_id),
    )
    if claim is None:
        return
    try:
        increases = sum(
            item.current.amount > item.previous.amount for item in batch.items
        )
        decreases = sum(
            item.current.amount < item.previous.amount for item in batch.items
        )
        observed = datetime.fromtimestamp(batch.summary.created_at, UTC).isoformat()
        description = (
            f"{batch.item_count} confirmed price changes: {increases} increases, {decreases} decreases.\n"
            f"Observed: {observed}\nBatch: {batch.batch_id} / {batch.fingerprint[:16]}\n"
            "This summary covers the stored observations."
        )
        if plan.additional_pending:
            description += f"\n{plan.additional_pending} additional changes remain pending for future scans."
        message = WebhookMessage(
            feed=feed,
            source_title=source_title[:160],
            entry=EntryData(
                title="Price change summary",
                link=feed.url,
                description=description,
                author="",
                timestamp=observed,
            ),
        )
        if dependencies.delivery.is_shutdown_requested():
            return
        result = dependencies.sender.send(message, dependencies.delivery.sleep)
        if result is DiscordDeliveryResult.DELIVERED:
            dependencies.sqlite_retry_policy.execute(
                partial(dependencies.snapshots.record_price_summary_delivery, claim),
            )
            finish_price_delivery(
                dependencies.snapshots,
                feed.id,
                plan,
                batch.summary.available_count,
            )
        else:
            record_price_health(
                dependencies.snapshots,
                feed.id,
                "failed",
                "AutomaticSummaryRetryPending",
                batch.item_count,
            )
    finally:
        dependencies.sqlite_retry_policy.execute(
            partial(dependencies.snapshots.release_price_summary_attempt, claim),
        )


def _send_claimed_price_change(
    dependencies: PriceChangeDeliveryDependencies,
    message: WebhookMessage,
    claim: PriceDeliveryClaim | None,
) -> DiscordDeliveryResult:
    try:
        result = dependencies.sender.send(message, dependencies.delivery.sleep)
    except BaseException:
        if claim is not None:
            dependencies.sqlite_retry_policy.execute(
                partial(
                    dependencies.snapshots.release_price_delivery_attempt,
                    claim,
                ),
            )
        raise
    if result is not DiscordDeliveryResult.DELIVERED and claim is not None:
        dependencies.sqlite_retry_policy.execute(
            partial(dependencies.snapshots.release_price_delivery_attempt, claim),
        )
    return result


def _wait_between_price_deliveries(
    delivery: PriceAlertDelivery,
    delay_before_next: bool,
) -> bool:
    if delay_before_next and delivery.delay_between_posts > 0:
        return delivery.sleep(delivery.delay_between_posts)
    return True


def _claim_price_change(
    dependencies: PriceChangeDeliveryDependencies,
    plan: PriceDeliveryPlan,
    product_id: str,
) -> PriceDeliveryClaim | None:
    claim = None
    if plan.batch_id is not None:
        claim = dependencies.sqlite_retry_policy.execute(
            partial(
                dependencies.snapshots.claim_price_delivery_attempt,
                plan.batch_id,
                product_id,
            ),
        )
        if claim is None:
            return None
    return claim


def _handle_price_delivery_result(
    dependencies: PriceChangeDeliveryDependencies,
    snapshot: PriceSnapshot,
    claim: PriceDeliveryClaim | None,
    result: DiscordDeliveryResult,
) -> tuple[bool, bool]:
    """Acknowledge a delivered snapshot and decide whether to continue and delay."""
    match result:
        case DiscordDeliveryResult.DELIVERED:
            dependencies.sqlite_retry_policy.execute(
                partial(
                    persist_price_delivery,
                    dependencies.snapshots,
                    claim,
                    snapshot,
                ),
            )
            return True, True
        case DiscordDeliveryResult.FAILED:
            return not dependencies.delivery.is_shutdown_requested(), False
        case DiscordDeliveryResult.INTERRUPTED:
            return False, False
        case unreachable:
            assert_never(unreachable)
