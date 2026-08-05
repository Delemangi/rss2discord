"""Construct sanitized callable price jobs for the generic runtime scheduler."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, runtime_checkable

from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender, SleepCallback
from .fetch_errors import FeedFetchError
from .price_monitor_builders import (
    DEFAULT_PRICE_MONITOR_FACTORIES,
    AnhochPriceMonitorFactory,
    DDStorePriceMonitorFactory,
    Gjirafa50PriceMonitorFactory,
    HivetecPriceMonitorFactory,
    NeksioPriceMonitorFactory,
    NeptunPriceMonitorFactory,
    Pazar3PriceMonitorFactory,
    PriceMonitor,
    PriceMonitorFactories,
    Reklama5PriceMonitorFactory,
    SetecPriceMonitorFactory,
    SharedPriceMonitorDependencies,
    build_provider_price_monitor,
)
from .retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from .scheduler import ScheduledJob
from .transports.pazar3_pacing import Pazar3RequestPacer
from .transports.price_monitor import PriceAlertDelivery

logger = logging.getLogger(__name__)


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
    *,
    anhoch_monitor_factory: AnhochPriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.anhoch,
    neksio_monitor_factory: NeksioPriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.neksio,
    neptun_monitor_factory: NeptunPriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.neptun,
    pazar3_monitor_factory: Pazar3PriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.pazar3,
    reklama5_monitor_factory: Reklama5PriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.reklama5,
    setec_monitor_factory: SetecPriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.setec,
    ddstore_monitor_factory: DDStorePriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.ddstore,
    hivetec_monitor_factory: HivetecPriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.hivetec,
    gjirafa50_monitor_factory: Gjirafa50PriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.gjirafa50,
) -> tuple[ScheduledJob, ...]:
    """Create one independent callable job for every enabled price-monitor feed."""
    jobs: list[ScheduledJob] = []
    retry_sleep = _RetrySleepAdapter(dependencies.sleep)
    pazar3_pacer = dependencies.pazar3_pacer or Pazar3RequestPacer(time.monotonic)
    factories = PriceMonitorFactories(
        anhoch=anhoch_monitor_factory,
        ddstore=ddstore_monitor_factory,
        hivetec=hivetec_monitor_factory,
        gjirafa50=gjirafa50_monitor_factory,
        neksio=neksio_monitor_factory,
        neptun=neptun_monitor_factory,
        pazar3=pazar3_monitor_factory,
        reklama5=reklama5_monitor_factory,
        setec=setec_monitor_factory,
    )
    for feed in config.feeds:
        interval = feed.price_check_interval
        if interval is None:
            continue
        shared_dependencies = _shared_monitor_dependencies(
            feed,
            dependencies,
            retry_sleep,
            pazar3_pacer,
        )
        monitor = build_provider_price_monitor(feed, shared_dependencies, factories)
        if monitor is None:
            continue
        close = monitor.close if isinstance(monitor, _ClosablePriceMonitor) else None
        jobs.append(
            ScheduledJob(
                interval,
                partial(_scan_price_monitor, monitor, feed.id),
                close,
            ),
        )
    return tuple(jobs)


def _shared_monitor_dependencies(
    feed: FeedConfig,
    dependencies: PriceJobDependencies,
    retry_sleep: _RetrySleepAdapter,
    pazar3_pacer: Pazar3RequestPacer,
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
    )


def _scan_price_monitor(monitor: PriceMonitor, feed_id: str) -> None:
    try:
        monitor.scan()
    except FeedFetchInterruptedError:
        return
    except SQLiteRetryInterruptedError:
        return
    except FeedFetchError as error:
        logger.exception(
            "Price scan failed for feed %s (%s)",
            feed_id,
            error.cause_type,
        )
    except sqlite3.Error as error:
        logger.exception(
            "Price scan persistence failed for feed %s (%s)",
            feed_id,
            type(error).__name__,
        )
    except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
        logger.exception(
            "Unexpected price scan failure for feed %s (%s)",
            feed_id,
            type(error).__name__,
            exc_info=RuntimeError(type(error).__name__).with_traceback(
                error.__traceback__,
            ),
        )


def _log_fetch_retry(feed_id: str, error: FeedFetchError, delay: float) -> None:
    logger.warning(
        "Price scan fetch retry for feed %s in %.1f seconds (%s)",
        feed_id,
        delay,
        error.cause_type,
    )


def _log_persistence_retry(feed_id: str, error: sqlite3.Error, delay: float) -> None:
    logger.warning(
        "Price scan persistence retry for feed %s in %.1f seconds (%s)",
        feed_id,
        delay,
        type(error).__name__,
    )
