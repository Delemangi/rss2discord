"""Construct sanitized callable price jobs for the generic runtime scheduler."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from .configuration import AppConfig, FeedConfig
from .delivery_store import DeliveryStore
from .discord.client import DiscordSender, SleepCallback
from .fetch_errors import FeedFetchError
from .price_monitor_builders import (
    DEFAULT_PRICE_MONITOR_FACTORIES,
    AnhochPriceMonitorFactory,
    CCCenterPriceMonitorFactory,
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
    TechnomarketPriceMonitorFactory,
    build_provider_price_monitor,
)
from .recovery_models import HealthUpdate
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
    cccenter_monitor_factory: CCCenterPriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.cccenter,
    technomarket_monitor_factory: TechnomarketPriceMonitorFactory = DEFAULT_PRICE_MONITOR_FACTORIES.technomarket,
) -> tuple[ScheduledJob, ...]:
    """Create one independent callable job for every enabled price-monitor feed."""
    jobs: list[ScheduledJob] = []
    retry_sleep = _RetrySleepAdapter(dependencies.sleep)
    pazar3_pacer = dependencies.pazar3_pacer or Pazar3RequestPacer(time.monotonic)
    factories = PriceMonitorFactories(
        anhoch=anhoch_monitor_factory,
        cccenter=cccenter_monitor_factory,
        ddstore=ddstore_monitor_factory,
        hivetec=hivetec_monitor_factory,
        gjirafa50=gjirafa50_monitor_factory,
        neksio=neksio_monitor_factory,
        neptun=neptun_monitor_factory,
        pazar3=pazar3_monitor_factory,
        reklama5=reklama5_monitor_factory,
        setec=setec_monitor_factory,
        technomarket=technomarket_monitor_factory,
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
            _cooldown_peer_ids(feed, config.feeds),
        )
        monitor = build_provider_price_monitor(feed, shared_dependencies, factories)
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


def _origin(url: str) -> tuple[str, str | None, int | None] | None:
    try:
        parts = urlsplit(url)
        origin = (
            parts.scheme,
            parts.hostname,
            parts.port or (443 if parts.scheme == "https" else 80),
        )
    except ValueError:
        return None
    return origin


def feed_is_blocked(
    feed: FeedConfig,
    feeds: tuple[FeedConfig, ...],
    store: DeliveryStore,
    now: int,
) -> bool:
    """Persisted feed cooldowns also cover configured peers on the same origin."""
    return any(
        (store.get_blocked_until(feed_id) or 0) > now
        for feed_id in _cooldown_peer_ids(feed, feeds)
    )


def _cooldown_peer_ids(
    feed: FeedConfig,
    feeds: tuple[FeedConfig, ...],
) -> tuple[str, ...]:
    origin = _origin(feed.url)
    # Bounded by the configured feeds; only IDs cross into worker dependencies.
    return tuple(
        dict.fromkeys(
            (
                feed.id,
                *(
                    peer.id
                    for peer in feeds
                    if origin is not None and _origin(peer.url) == origin
                ),
            ),
        ),
    )


def record_runtime_health(store: DeliveryStore, update: HealthUpdate) -> None:
    """Only transitions/reminders log; diagnostic payloads contain no URLs."""
    try:
        notice = store.record_health(update, reminder_seconds=21600)
    except sqlite3.Error as error:
        logger.log(
            logging.ERROR,
            "Health persistence failed for feed %s (%s)",
            update.feed_id,
            type(error).__name__,
        )
        return
    if notice.should_log:
        level = logging.ERROR if update.state in {"failed", "blocked"} else logging.INFO
        logger.log(
            level,
            "Feed %s %s health=%s cause=%s recovered=%s",
            update.feed_id,
            update.job_kind,
            update.state,
            update.cause,
            notice.recovered,
        )


def record_fetch_failure(
    store: DeliveryStore,
    feed_id: str,
    job_kind: Literal["ordinary", "price"],
    error: Exception,
    attempted_at: int,
    *,
    duration_ms: int,
) -> None:
    cause = safe_error_cause(error)
    blocked = isinstance(error, FeedFetchError) and (
        error.cause_type in {"BotChallenge", "AccessChallenge"}
        or error.status_code == 403
    )
    record_runtime_health(
        store,
        HealthUpdate(
            feed_id=feed_id,
            job_kind=job_kind,
            state="blocked" if blocked else "failed",
            cause=cause,
            attempted_at=attempted_at,
            success=False,
            nonempty=False,
            item_count=0,
            duration_ms=duration_ms,
            scheduler_lag_ms=0,
        ),
    )


def safe_error_cause(error: Exception) -> str:
    cause = (
        error.cause_type if isinstance(error, FeedFetchError) else type(error).__name__
    )
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", cause):
        return "FetchError"
    if (
        isinstance(error, FeedFetchError)
        and isinstance(error.status_code, int)
        and 100 <= error.status_code <= 599
    ):
        return f"{cause} HTTP {error.status_code}"
    return cause


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
