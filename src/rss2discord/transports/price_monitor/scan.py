"""Shared orchestration for provider price scans."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_safety import MAX_INLINE_PRICE_CHANGES
from rss2discord.recovery_models import PriceChangeRecord, PriceSnapshot
from rss2discord.retries import FeedFetchInterruptedError

from .comparison import diff_price_snapshots
from .contracts import (
    PriceChangeDeliveryDependencies,
    PriceDeliveryOutcome,
    PriceDeliveryPlan,
)
from .delivery import deliver_price_changes, drain_pending_price_notifications
from .planning import (
    finish_price_delivery,
    prepare_price_delivery,
    record_price_health,
    validate_price_currencies,
)

NO_SNAPSHOT_LIMIT: Final = 1_000_000


@dataclass(frozen=True, slots=True)
class PriceObservation[ProductT]:
    product: ProductT
    snapshot: PriceSnapshot


@dataclass(frozen=True, slots=True)
class PriceChangeView:
    previous: PriceSnapshot
    current: PriceSnapshot
    message: WebhookMessage


def run_price_scan[ProductT](
    *,
    feed: FeedConfig,
    dependencies: PriceChangeDeliveryDependencies,
    provider: str,
    label: str,
    observations: Sequence[PriceObservation[ProductT]],
    message_for: Callable[[ProductT, PriceSnapshot, PriceSnapshot], WebhookMessage],
    snapshot_limit: int,
    confirm: Callable[
        [Sequence[PriceObservation[ProductT]]],
        Sequence[PriceObservation[ProductT]],
    ]
    | None = None,
    confirmation_limit: int | None = None,
) -> None:
    """Load baselines, diff observations, confirm, and deliver one provider scan."""
    if snapshot_limit < 1:
        raise ValueError("snapshot_limit must be positive")
    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError
    persisted = dependencies.sqlite_retry_policy.execute(
        lambda: dependencies.snapshots.load_price_snapshots(
            feed.id,
            limit=snapshot_limit + 1,
        ),
    )
    if len(persisted) > snapshot_limit:
        raise FeedFetchError(label, "SnapshotLimitExceeded")
    by_id = {snapshot.product_id: snapshot for snapshot in persisted}

    current: dict[str, PriceSnapshot] = {}
    observation_by_id: dict[str, PriceObservation[ProductT]] = {}
    for observation in observations:
        current[observation.snapshot.product_id] = observation.snapshot
        observation_by_id[observation.snapshot.product_id] = observation
    if len(set(by_id) | set(current)) > snapshot_limit:
        raise FeedFetchError(label, "SnapshotLimitExceeded")
    validate_price_currencies(
        provider=provider,
        current=current,
        persisted=by_id,
    )

    silent, records = diff_price_snapshots(current.values(), by_id)
    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError
    if not records:
        if silent:
            dependencies.sqlite_retry_policy.execute(
                lambda: dependencies.snapshots.upsert_price_snapshots(silent),
            )
        if drain_pending_price_notifications(dependencies, feed) is (
            PriceDeliveryOutcome.DELIVERED
        ):
            finish_price_delivery(
                dependencies.snapshots,
                feed.id,
                PriceDeliveryPlan(),
                len(current),
            )
        return

    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError

    records_by_id = {record.product_id: record for record in records}
    change_observations = tuple(
        observation_by_id[record.product_id] for record in records
    )
    bulk = len(records) > MAX_INLINE_PRICE_CHANGES
    if not bulk and silent:
        dependencies.sqlite_retry_policy.execute(
            lambda: dependencies.snapshots.upsert_price_snapshots(silent),
        )
        if dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
    additional_pending = 0
    if confirm is None:
        confirmed = change_observations
    elif bulk and confirmation_limit is not None:
        selected_ids = dependencies.sqlite_retry_policy.execute(
            lambda: dependencies.snapshots.select_price_confirmation_subset(
                feed_id=feed.id,
                product_ids=tuple(record.product_id for record in records),
                limit=confirmation_limit,
            ),
        )
        candidates = tuple(
            observation_by_id[product_id]
            for product_id in selected_ids
            if product_id in observation_by_id
        )
        confirmed = tuple(confirm(candidates))
        additional_pending = len(records) - len(confirmed)
        if not confirmed:
            record_price_health(
                dependencies.snapshots,
                feed.id,
                "failed",
                "PriceConfirmationDeferred",
                len(current),
            )
            return
    else:
        confirmed = tuple(confirm(change_observations))
        additional_pending = 0

    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError

    views = tuple(
        PriceChangeView(
            previous=records_by_id[observation.snapshot.product_id].previous,
            current=observation.snapshot,
            message=message_for(
                observation.product,
                records_by_id[observation.snapshot.product_id].previous,
                observation.snapshot,
            ),
        )
        for observation in confirmed
    )
    confirmed_records = tuple(
        PriceChangeRecord(
            observation.snapshot.product_id,
            records_by_id[observation.snapshot.product_id].previous,
            observation.snapshot,
        )
        for observation in confirmed
    )
    plan = dependencies.sqlite_retry_policy.execute(
        lambda: prepare_price_delivery(
            store=dependencies.snapshots,
            feed_id=feed.id,
            provider=provider,
            changes=confirmed_records,
            current=current,
            persisted=by_id,
            catalog_count=len(observations),
            force_summary=bulk,
            additional_pending=additional_pending,
        ),
    )
    if plan.bulk:
        deliver_price_changes(
            views,
            dependencies,
            lambda view: view.message,
            plan=plan,
            feed=feed,
            source_title=feed.name or label,
            baseline_extra=silent,
        )
        return
    outcome = deliver_price_changes(
        views,
        dependencies,
        lambda view: view.message,
        plan=plan,
        feed=feed,
        source_title=feed.name or label,
    )
    if outcome is PriceDeliveryOutcome.FAILED:
        pending = dependencies.sqlite_retry_policy.execute(
            lambda: dependencies.snapshots.count_pending_price_notifications(
                feed.id,
            ),
        )
        record_price_health(
            dependencies.snapshots,
            feed.id,
            "failed",
            "DeliveryRetryPending",
            pending or len(records),
        )
        return
    if outcome is PriceDeliveryOutcome.INTERRUPTED:
        return
    if len(confirmed) < len(records):
        record_price_health(
            dependencies.snapshots,
            feed.id,
            "failed",
            "PriceConfirmationDeferred",
            len(current),
        )
    else:
        finish_price_delivery(
            dependencies.snapshots,
            feed.id,
            plan,
            len(current),
        )
