"""Construct sanitized callable price jobs for the generic runtime scheduler."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, runtime_checkable

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordSender, SleepCallback
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_monitor_builders import (
    PriceMonitor,
    SharedPriceMonitorDependencies,
    build_provider_price_monitor,
)
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import PriceAlertDelivery

from .health import (
    cooldown_peer_ids,
    feed_is_blocked,
    record_fetch_failure,
    safe_error_cause,
)
from .scheduler import ScheduledJob

logger = logging.getLogger("rss2discord.price_runtime")


@runtime_checkable
class _ClosablePriceMonitor(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PriceJobDependencies:
    """Runtime collaborators shared by all configured price monitors."""

    store: DeliveryStore
    sender: DiscordSender
    sleep: SleepCallback
    delay_between_posts: float
    is_shutdown_requested: Callable[[], bool]
    pazar3_pacer: Pazar3RequestPacer | None = None


class _RetrySleepAdapter:
    def __init__(self, sleep: SleepCallback) -> None:
        self._sleep = sleep

    def __call__(self, seconds: float) -> bool:
        return self._sleep(seconds)


def build_price_jobs(
    config: AppConfig,
    dependencies: PriceJobDependencies,
) -> tuple[ScheduledJob, ...]:
    """Create one independent callable job for every enabled price-monitor feed."""
    jobs: list[ScheduledJob] = []
    retry_sleep = _RetrySleepAdapter(dependencies.sleep)
    pazar3_pacer = dependencies.pazar3_pacer or Pazar3RequestPacer(time.monotonic)
    for feed in config.feeds:
        interval = feed.price_check_interval
        if interval is None:
            continue
        shared_dependencies = _shared_monitor_dependencies(
            feed,
            dependencies,
            retry_sleep,
            pazar3_pacer,
            cooldown_peer_ids(feed, config.feeds),
        )
        monitor = build_provider_price_monitor(feed, shared_dependencies)
        if monitor is None:
            continue
        close = monitor.close if isinstance(monitor, _ClosablePriceMonitor) else None
        jobs.append(
            ScheduledJob(
                feed.id,
                "price",
                interval,
                partial(
                    _scan_price_monitor,
                    monitor,
                    feed,
                    config.feeds,
                    dependencies.store,
                ),
                close,
            ),
        )
    return tuple(jobs)


def _shared_monitor_dependencies(
    feed: FeedConfig,
    dependencies: PriceJobDependencies,
    retry_sleep: _RetrySleepAdapter,
    pazar3_pacer: Pazar3RequestPacer,
    cooldown_peer_feed_ids: tuple[str, ...],
) -> SharedPriceMonitorDependencies:
    return SharedPriceMonitorDependencies(
        snapshots=dependencies.store,
        sender=dependencies.sender,
        fetch_retry_policy=FetchRetryPolicy(
            sleep=retry_sleep,
            on_retry=partial(_log_fetch_retry, feed.id),
        ),
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=retry_sleep,
            on_retry=partial(_log_persistence_retry, feed.id),
        ),
        delivery=PriceAlertDelivery(
            sleep=dependencies.sleep,
            delay_between_posts=dependencies.delay_between_posts,
            is_shutdown_requested=dependencies.is_shutdown_requested,
        ),
        pazar3_pacer=pazar3_pacer,
        cooldown_peer_feed_ids=cooldown_peer_feed_ids,
    )


def _scan_price_monitor(
    monitor: PriceMonitor,
    feed: FeedConfig,
    feeds: tuple[FeedConfig, ...],
    store: DeliveryStore,
) -> None:
    attempted_at = int(time.time())
    started_at = time.monotonic()
    try:
        if feed_is_blocked(feed, feeds, store, attempted_at):
            return
        monitor.scan()
    except FeedFetchInterruptedError:
        return
    except SQLiteRetryInterruptedError:
        return
    except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
        record_fetch_failure(
            store,
            feed.id,
            "price",
            error,
            attempted_at,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        )
    # A successful return may mean quarantine, a background scan, or no work.
    # Adapters own successful/domain health; scheduler timing is recorded
    # separately and must never clear a quarantined or recovery-required state.


def _log_fetch_retry(feed_id: str, error: FeedFetchError, delay: float) -> None:
    logger.warning(
        "Price scan fetch retry for feed %s in %.1f seconds (%s)",
        feed_id,
        delay,
        safe_error_cause(error),
    )


def _log_persistence_retry(feed_id: str, error: sqlite3.Error, delay: float) -> None:
    logger.warning(
        "Price scan persistence retry for feed %s in %.1f seconds (%s)",
        feed_id,
        delay,
        type(error).__name__,
    )
