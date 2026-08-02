"""Construct sanitized callable price jobs for the generic runtime scheduler."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, assert_never

from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender, SleepCallback
from .fetch_errors import FeedFetchError
from .retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from .scheduler import ScheduledJob
from .transports.anhoch_catalog import AnhochCatalogClient
from .transports.anhoch_price_monitor import (
    AnhochPriceMonitor,
    AnhochPriceMonitorDependencies,
)
from .transports.ddstore_catalog import DDStoreCatalogClient
from .transports.ddstore_price_monitor import (
    DDStorePriceMonitor,
    DDStorePriceMonitorDependencies,
)
from .transports.neksio_catalog import NeksioCatalogClient
from .transports.neksio_price_monitor import (
    NeksioPriceMonitor,
    NeksioPriceMonitorDependencies,
)
from .transports.neptun_catalog import NeptunCatalogClient
from .transports.neptun_price_monitor import (
    NeptunPriceMonitor,
    NeptunPriceMonitorDependencies,
)
from .transports.price_monitor import PriceAlertDelivery
from .transports.setec_catalog import SetecCatalogClient
from .transports.setec_price_monitor import (
    SetecPriceMonitor,
    SetecPriceMonitorDependencies,
)

logger = logging.getLogger(__name__)


class PriceMonitor(Protocol):
    """Perform one full-catalog price scan."""

    def scan(self) -> None: ...


type AnhochPriceMonitorFactory = Callable[
    [FeedConfig, AnhochPriceMonitorDependencies],
    PriceMonitor,
]
type NeksioPriceMonitorFactory = Callable[
    [FeedConfig, NeksioPriceMonitorDependencies],
    PriceMonitor,
]
type NeptunPriceMonitorFactory = Callable[
    [FeedConfig, NeptunPriceMonitorDependencies],
    PriceMonitor,
]
type SetecPriceMonitorFactory = Callable[
    [FeedConfig, SetecPriceMonitorDependencies],
    PriceMonitor,
]
type DDStorePriceMonitorFactory = Callable[
    [FeedConfig, DDStorePriceMonitorDependencies],
    PriceMonitor,
]


@dataclass(frozen=True, slots=True)
class PriceJobDependencies:
    """Runtime collaborators shared by all configured price monitors."""

    store: DeliveryStore
    sender: DiscordSender
    sleep: SleepCallback
    delay_between_posts: float
    is_shutdown_requested: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class _SharedPriceMonitorDependencies:
    snapshots: DeliveryStore
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery


class _RetrySleepAdapter:
    def __init__(self, sleep: SleepCallback) -> None:
        self._sleep = sleep

    def __call__(self, seconds: float) -> bool:
        return self._sleep(seconds)


def build_price_jobs(
    config: AppConfig,
    dependencies: PriceJobDependencies,
    *,
    anhoch_monitor_factory: AnhochPriceMonitorFactory = AnhochPriceMonitor,
    neksio_monitor_factory: NeksioPriceMonitorFactory = NeksioPriceMonitor,
    neptun_monitor_factory: NeptunPriceMonitorFactory = NeptunPriceMonitor,
    setec_monitor_factory: SetecPriceMonitorFactory = SetecPriceMonitor,
    ddstore_monitor_factory: DDStorePriceMonitorFactory = DDStorePriceMonitor,
) -> tuple[ScheduledJob, ...]:
    """Create one independent callable job for every enabled price-monitor feed."""
    jobs: list[ScheduledJob] = []
    retry_sleep = _RetrySleepAdapter(dependencies.sleep)
    for feed in config.feeds:
        match feed.strategy:
            case "anhoch":
                interval = feed.price_check_interval
                if interval is None:
                    continue
                shared_dependencies = _shared_monitor_dependencies(
                    feed,
                    dependencies,
                    retry_sleep,
                )
                monitor = anhoch_monitor_factory(
                    feed,
                    AnhochPriceMonitorDependencies(
                        catalog=AnhochCatalogClient(),
                        snapshots=shared_dependencies.snapshots,
                        sender=shared_dependencies.sender,
                        fetch_retry_policy=shared_dependencies.fetch_retry_policy,
                        sqlite_retry_policy=shared_dependencies.sqlite_retry_policy,
                        delivery=shared_dependencies.delivery,
                    ),
                )
            case "neksio":
                interval = feed.price_check_interval
                if interval is None:
                    continue
                shared_dependencies = _shared_monitor_dependencies(
                    feed,
                    dependencies,
                    retry_sleep,
                )
                monitor = neksio_monitor_factory(
                    feed,
                    NeksioPriceMonitorDependencies(
                        catalog=NeksioCatalogClient(),
                        snapshots=shared_dependencies.snapshots,
                        sender=shared_dependencies.sender,
                        fetch_retry_policy=shared_dependencies.fetch_retry_policy,
                        sqlite_retry_policy=shared_dependencies.sqlite_retry_policy,
                        delivery=shared_dependencies.delivery,
                    ),
                )
            case "neptun":
                interval = feed.price_check_interval
                if interval is None:
                    continue
                shared_dependencies = _shared_monitor_dependencies(
                    feed,
                    dependencies,
                    retry_sleep,
                )
                monitor = neptun_monitor_factory(
                    feed,
                    NeptunPriceMonitorDependencies(
                        catalog=NeptunCatalogClient(),
                        snapshots=shared_dependencies.snapshots,
                        sender=shared_dependencies.sender,
                        fetch_retry_policy=shared_dependencies.fetch_retry_policy,
                        sqlite_retry_policy=shared_dependencies.sqlite_retry_policy,
                        delivery=shared_dependencies.delivery,
                    ),
                )
            case "setec":
                interval = feed.price_check_interval
                if interval is None:
                    continue
                shared_dependencies = _shared_monitor_dependencies(
                    feed,
                    dependencies,
                    retry_sleep,
                )
                monitor = setec_monitor_factory(
                    feed,
                    SetecPriceMonitorDependencies(
                        catalog=SetecCatalogClient(),
                        snapshots=shared_dependencies.snapshots,
                        sender=shared_dependencies.sender,
                        fetch_retry_policy=shared_dependencies.fetch_retry_policy,
                        sqlite_retry_policy=shared_dependencies.sqlite_retry_policy,
                        delivery=shared_dependencies.delivery,
                    ),
                )
            case "ddstore":
                interval = feed.price_check_interval
                if interval is None:
                    continue
                shared_dependencies = _shared_monitor_dependencies(
                    feed,
                    dependencies,
                    retry_sleep,
                )
                monitor = ddstore_monitor_factory(
                    feed,
                    DDStorePriceMonitorDependencies(
                        catalog=DDStoreCatalogClient(),
                        snapshots=shared_dependencies.snapshots,
                        sender=shared_dependencies.sender,
                        fetch_retry_policy=shared_dependencies.fetch_retry_policy,
                        sqlite_retry_policy=shared_dependencies.sqlite_retry_policy,
                        delivery=shared_dependencies.delivery,
                    ),
                )
            case "rss" | "xenforo" | "itmk_oglasnik" | "reklama5":
                continue
            case unreachable:
                assert_never(unreachable)
        jobs.append(
            ScheduledJob(interval, partial(_scan_price_monitor, monitor, feed.id)),
        )
    return tuple(jobs)


def _shared_monitor_dependencies(
    feed: FeedConfig,
    dependencies: PriceJobDependencies,
    retry_sleep: _RetrySleepAdapter,
) -> _SharedPriceMonitorDependencies:
    return _SharedPriceMonitorDependencies(
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
