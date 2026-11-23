"""Shared orchestration for provider price scans."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Final

from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import (
    PriceChangeRecord,
    PriceSnapshot,
)
from rss2discord.retries import FeedFetchInterruptedError

from .comparison import diff_price_snapshots, diff_stock_snapshots
from .contracts import (
    PriceChangeDeliveryDependencies,
    PriceDeliveryOutcome,
)
from .delivery import deliver_price_changes, drain_pending_price_notifications
from .filters import split_price_alert_changes
from .planning import (
    finish_price_delivery,
    prepare_price_delivery,
    record_price_health,
    validate_price_currencies,
)
from .presentation import with_price_change_percentage, with_restock
from .timing import current_scan_started_at

NO_SNAPSHOT_LIMIT: Final = 1_000_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PriceObservation[ProductT]:
    product: ProductT
    snapshot: PriceSnapshot


@dataclass(frozen=True, slots=True)
class PriceChangeView:
    previous: PriceSnapshot
    current: PriceSnapshot
    message: WebhookMessage


def _in_observation_order(
    records: Sequence[PriceChangeRecord],
    current: Mapping[str, PriceSnapshot],
) -> tuple[PriceChangeRecord, ...]:
    """Order disjoint records the way the provider observed their products."""
    order = {product_id: index for index, product_id in enumerate(current)}
    return tuple(
        sorted(records, key=lambda record: order.get(record.product_id, len(order))),
    )


def _dedupe_snapshots(
    snapshots: Sequence[PriceSnapshot],
) -> tuple[PriceSnapshot, ...]:
    """Keep the first snapshot per product so a scan upserts it only once."""
    seen: set[str] = set()
    unique: list[PriceSnapshot] = []
    for snapshot in snapshots:
        if snapshot.product_id in seen:
            continue
        seen.add(snapshot.product_id)
        unique.append(snapshot)
    return tuple(unique)


def _view_for[ProductT](
    observation: PriceObservation[ProductT],
    record: PriceChangeRecord,
    restock_ids: Collection[str],
    message_for: Callable[[ProductT, PriceSnapshot, PriceSnapshot], WebhookMessage],
) -> PriceChangeView:
    """Build one card, marking restocks so they are not titled as price moves."""
    message = message_for(observation.product, record.previous, observation.snapshot)
    if record.product_id in restock_ids:
        message = with_restock(message)
    return PriceChangeView(
        previous=record.previous,
        current=observation.snapshot,
        message=with_price_change_percentage(
            message,
            record.previous,
            observation.snapshot,
        ),
    )


@dataclass(slots=True)
class _ScanCounters:
    """Mutable counters reported once per completed price scan."""

    observations: int = 0
    alerts: int = 0
    restocks: int = 0
    suppressed: int = 0
    unavailable: int = 0
    confirmed: int = 0
    bulk: bool = False


@dataclass(frozen=True, slots=True)
class _ScanDiff:
    """One scan's classified changes before delivery planning."""

    silent: tuple[PriceSnapshot, ...]
    alerting: tuple[PriceChangeRecord, ...]
    restock_records: tuple[PriceChangeRecord, ...]
    restock_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _ScanContext[ProductT]:
    """Stable collaborators and observations shared by one scan's stages."""

    feed: FeedConfig
    dependencies: PriceChangeDeliveryDependencies
    provider: str
    current: Mapping[str, PriceSnapshot]
    by_id: Mapping[str, PriceSnapshot]
    observation_by_id: Mapping[str, PriceObservation[ProductT]]
    message_for: Callable[[ProductT, PriceSnapshot, PriceSnapshot], WebhookMessage]
    catalog_count: int


def _log_scan(
    *,
    feed_id: str,
    provider: str,
    counters: _ScanCounters,
    outcome: str,
    elapsed_seconds: float,
    error: str | None = None,
) -> None:
    detail = f" error={error}" if error is not None else ""
    logger.info(
        "Price scan feed=%s provider=%s observations=%d alerts=%d restocks=%d "
        "suppressed=%d unavailable=%d confirmed=%d bulk=%s outcome=%s%s "
        "elapsed_ms=%d",
        feed_id,
        provider,
        counters.observations,
        counters.alerts,
        counters.restocks,
        counters.suppressed,
        counters.unavailable,
        counters.confirmed,
        str(counters.bulk).lower(),
        outcome,
        detail,
        round(elapsed_seconds * 1000),
    )


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
    allow_currency_changes: bool = False,
    started_at: float | None = None,
    preloaded_snapshots: Sequence[PriceSnapshot] | None = None,
) -> None:
    """Load baselines, diff observations, confirm, and deliver one provider scan."""
    started = started_at if started_at is not None else current_scan_started_at()
    if started is None:
        started = time.perf_counter()
    counters = _ScanCounters()
    try:
        outcome = _run_price_scan(
            feed=feed,
            dependencies=dependencies,
            provider=provider,
            label=label,
            observations=observations,
            message_for=message_for,
            snapshot_limit=snapshot_limit,
            confirm=confirm,
            confirmation_limit=confirmation_limit,
            allow_currency_changes=allow_currency_changes,
            counters=counters,
            preloaded_snapshots=preloaded_snapshots,
        )
    except FeedFetchInterruptedError:
        _log_scan(
            feed_id=feed.id,
            provider=provider,
            counters=counters,
            outcome="interrupted",
            elapsed_seconds=time.perf_counter() - started,
        )
        raise
    except FeedFetchError as error:
        _log_scan(
            feed_id=feed.id,
            provider=provider,
            counters=counters,
            outcome="error",
            elapsed_seconds=time.perf_counter() - started,
            error=type(error).__name__,
        )
        raise
    _log_scan(
        feed_id=feed.id,
        provider=provider,
        counters=counters,
        outcome=outcome.value,
        elapsed_seconds=time.perf_counter() - started,
    )


def _run_price_scan[ProductT](
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
    allow_currency_changes: bool = False,
    counters: _ScanCounters,
    preloaded_snapshots: Sequence[PriceSnapshot] | None = None,
) -> PriceDeliveryOutcome:
    by_id = _load_persisted_snapshots(
        dependencies,
        feed,
        snapshot_limit,
        label,
        preloaded_snapshots,
    )
    current, observation_by_id = _current_observations(
        observations,
        by_id,
        provider=provider,
        label=label,
        snapshot_limit=snapshot_limit,
        allow_currency_changes=allow_currency_changes,
        counters=counters,
    )
    dependencies.sqlite_retry_policy.execute(
        partial(
            dependencies.snapshots.touch_price_snapshots,
            feed.id,
            tuple(current),
        ),
    )
    context = _ScanContext(
        feed=feed,
        dependencies=dependencies,
        provider=provider,
        current=current,
        by_id=by_id,
        observation_by_id=observation_by_id,
        message_for=message_for,
        catalog_count=len(observations),
    )
    diff = _collect_scan_diff(context, counters)
    records, silent = _delivery_records(diff, current)
    if not records:
        return _finish_without_records(context, silent)
    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError

    bulk = len(records) > feed.price_inline_limit
    counters.bulk = bulk
    if not bulk and silent:
        dependencies.sqlite_retry_policy.execute(
            lambda: dependencies.snapshots.upsert_price_snapshots(silent),
        )
        if dependencies.delivery.is_shutdown_requested():
            raise FeedFetchInterruptedError
    confirmation = _confirm_observations(
        context,
        confirm,
        records,
        bulk=bulk,
        confirmation_limit=confirmation_limit,
    )
    if confirmation is None:
        return PriceDeliveryOutcome.FAILED
    confirmed, additional_pending = confirmation
    counters.confirmed = len(confirmed)

    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError

    return _deliver_confirmed_changes(
        context,
        records,
        silent,
        diff.restock_ids,
        confirmed,
        additional_pending=additional_pending,
        bulk=bulk,
        allow_currency_changes=allow_currency_changes,
    )


def _load_persisted_snapshots(
    dependencies: PriceChangeDeliveryDependencies,
    feed: FeedConfig,
    snapshot_limit: int,
    label: str,
    preloaded: Sequence[PriceSnapshot] | None = None,
) -> dict[str, PriceSnapshot]:
    if snapshot_limit < 1:
        raise ValueError("snapshot_limit must be positive")
    if dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError
    persisted: Sequence[PriceSnapshot]
    if preloaded is None:
        persisted = dependencies.sqlite_retry_policy.execute(
            lambda: dependencies.snapshots.load_price_snapshots(
                feed.id,
                limit=snapshot_limit + 1,
            ),
        )
    else:
        persisted = preloaded
    if len(persisted) > snapshot_limit:
        raise FeedFetchError(label, "SnapshotLimitExceeded")
    return {snapshot.product_id: snapshot for snapshot in persisted}


def _current_observations[ProductT](
    observations: Sequence[PriceObservation[ProductT]],
    by_id: Mapping[str, PriceSnapshot],
    *,
    provider: str,
    label: str,
    snapshot_limit: int,
    allow_currency_changes: bool,
    counters: _ScanCounters,
) -> tuple[dict[str, PriceSnapshot], dict[str, PriceObservation[ProductT]]]:
    current: dict[str, PriceSnapshot] = {}
    observation_by_id: dict[str, PriceObservation[ProductT]] = {}
    for observation in observations:
        current[observation.snapshot.product_id] = observation.snapshot
        observation_by_id[observation.snapshot.product_id] = observation
    counters.observations = len(current)
    if len(set(by_id) | set(current)) > snapshot_limit:
        raise FeedFetchError(label, "SnapshotLimitExceeded")
    if not allow_currency_changes:
        validate_price_currencies(
            provider=provider,
            current=current,
            persisted=by_id,
        )
    return current, observation_by_id


def _collect_scan_diff[ProductT](
    context: _ScanContext[ProductT],
    counters: _ScanCounters,
) -> _ScanDiff:
    silent, price_records = diff_price_snapshots(
        context.current.values(),
        context.by_id,
    )
    stock_silent, restock_records = diff_stock_snapshots(
        context.current.values(),
        context.by_id,
    )
    if context.dependencies.delivery.is_shutdown_requested():
        raise FeedFetchInterruptedError
    silent = (*silent, *stock_silent)
    if not context.feed.restock_alerts:
        # Availability still advances the baseline, it just never alerts.
        silent = (*silent, *(record.current for record in restock_records))
        restock_records = ()
    alerting, suppressed = split_price_alert_changes(price_records, context.feed)
    restock_ids = {record.product_id for record in restock_records}
    # A restock carries the same current snapshot, so it takes precedence over
    # whatever the price diff detected for that product.
    alerting = tuple(
        record for record in alerting if record.product_id not in restock_ids
    )
    suppressed = tuple(
        record for record in suppressed if record.product_id not in restock_ids
    )
    # A filtered change still advances its baseline so the same move is not
    # re-evaluated forever. Silent snapshots already ride the inline/bulk paths.
    silent = (*silent, *(record.current for record in suppressed))
    # An out-of-stock observation cannot be a price alert; keep only its
    # baseline current so a later restock reports against the latest price.
    unavailable = tuple(
        record for record in alerting if record.current.available is False
    )
    alerting = tuple(
        record for record in alerting if record.current.available is not False
    )
    silent = (*silent, *(record.current for record in unavailable))
    counters.alerts = len(alerting)
    counters.restocks = len(restock_records)
    counters.suppressed = len(suppressed)
    counters.unavailable = len(unavailable)
    return _ScanDiff(
        silent=silent,
        alerting=alerting,
        restock_records=restock_records,
        restock_ids=frozenset(restock_ids),
    )


def _delivery_records(
    diff: _ScanDiff,
    current: Mapping[str, PriceSnapshot],
) -> tuple[tuple[PriceChangeRecord, ...], tuple[PriceSnapshot, ...]]:
    # Restocks bypass the price filters above and are never suppressed.
    records = _in_observation_order(
        (*diff.alerting, *diff.restock_records),
        current,
    )
    record_ids = {record.product_id for record in records}
    # A product that will alert persists through its delivery (or the outbox),
    # never as a pre-delivery baseline; otherwise a failed send would lose the
    # alert. One snapshot per product: the same product can be silent for
    # several reasons in one scan.
    silent = _dedupe_snapshots(
        [snapshot for snapshot in diff.silent if snapshot.product_id not in record_ids],
    )
    return records, silent


def _finish_without_records[ProductT](
    context: _ScanContext[ProductT],
    silent: Sequence[PriceSnapshot],
) -> PriceDeliveryOutcome:
    dependencies = context.dependencies
    if silent:
        dependencies.sqlite_retry_policy.execute(
            lambda: dependencies.snapshots.upsert_price_snapshots(silent),
        )
    outcome = drain_pending_price_notifications(dependencies, context.feed)
    if outcome is PriceDeliveryOutcome.DELIVERED:
        finish_price_delivery(
            dependencies.snapshots,
            context.feed.id,
            len(context.current),
        )
    return outcome


def _confirm_observations[ProductT](
    context: _ScanContext[ProductT],
    confirm: Callable[
        [Sequence[PriceObservation[ProductT]]],
        Sequence[PriceObservation[ProductT]],
    ]
    | None,
    records: Sequence[PriceChangeRecord],
    *,
    bulk: bool,
    confirmation_limit: int | None,
) -> tuple[tuple[PriceObservation[ProductT], ...], int] | None:
    observation_by_id = context.observation_by_id
    change_observations = tuple(
        observation_by_id[record.product_id] for record in records
    )
    if confirm is None:
        return change_observations, 0
    if not (bulk and confirmation_limit is not None):
        return tuple(confirm(change_observations)), 0
    dependencies = context.dependencies
    selected_ids = dependencies.sqlite_retry_policy.execute(
        lambda: dependencies.snapshots.select_price_confirmation_subset(
            feed_id=context.feed.id,
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
    if confirmed:
        return confirmed, len(records) - len(confirmed)
    record_price_health(
        dependencies.snapshots,
        context.feed.id,
        "failed",
        "PriceConfirmationDeferred",
        len(context.current),
    )
    return None


def _deliver_confirmed_changes[ProductT](
    context: _ScanContext[ProductT],
    records: Sequence[PriceChangeRecord],
    silent: Sequence[PriceSnapshot],
    restock_ids: Collection[str],
    confirmed: Sequence[PriceObservation[ProductT]],
    *,
    additional_pending: int,
    bulk: bool,
    allow_currency_changes: bool,
) -> PriceDeliveryOutcome:
    dependencies = context.dependencies
    feed = context.feed
    records_by_id = {record.product_id: record for record in records}
    views = tuple(
        _view_for(
            observation,
            records_by_id[observation.snapshot.product_id],
            restock_ids,
            context.message_for,
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
            provider=context.provider,
            changes=confirmed_records,
            current=context.current,
            persisted=context.by_id,
            catalog_count=context.catalog_count,
            inline_limit=feed.price_inline_limit,
            force_summary=bulk,
            additional_pending=additional_pending,
            allow_currency_changes=allow_currency_changes,
        ),
    )
    if plan.bulk:
        return deliver_price_changes(
            views,
            dependencies,
            lambda view: view.message,
            plan=plan,
            feed=feed,
            baseline_extra=silent,
        )
    outcome = deliver_price_changes(
        views,
        dependencies,
        lambda view: view.message,
        plan=plan,
        feed=feed,
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
        return PriceDeliveryOutcome.FAILED
    if outcome is PriceDeliveryOutcome.INTERRUPTED:
        return PriceDeliveryOutcome.INTERRUPTED
    if len(confirmed) < len(records):
        record_price_health(
            dependencies.snapshots,
            feed.id,
            "failed",
            "PriceConfirmationDeferred",
            len(context.current),
        )
        return PriceDeliveryOutcome.FAILED
    finish_price_delivery(
        dependencies.snapshots,
        feed.id,
        len(context.current),
    )
    return PriceDeliveryOutcome.DELIVERED
