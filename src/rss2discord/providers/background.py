"""Shared background price-scan worker and its dependency contract."""

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock, Thread
from typing import ClassVar, Protocol

import requests

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordSender, DiscordWebhookClient
from rss2discord.fetch_errors import FeedFetchError, safe_error_cause
from rss2discord.models import HealthUpdate
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceRecoveryStore,
    price_scan_timing,
)

logger = logging.getLogger(__name__)


class PriceMonitor(Protocol):
    def scan(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SharedPriceMonitorDependencies:
    snapshots: PriceRecoveryStore
    database_path: Path
    sender: DiscordSender
    fetch_retry_policy: FetchRetryPolicy
    sqlite_retry_policy: SQLiteRetryPolicy
    delivery: PriceAlertDelivery
    pazar3_pacer: Pazar3RequestPacer
    cooldown_peer_feed_ids: tuple[str, ...] = ()


PriceMonitorFactory = Callable[
    [FeedConfig, SharedPriceMonitorDependencies],
    PriceMonitor,
]


class BackgroundPriceMonitor:
    """Run a price scan in a worker with its own store and Discord session."""

    _scan_lock: ClassVar[Lock] = Lock()

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: SharedPriceMonitorDependencies,
        factory: PriceMonitorFactory,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies
        self._factory = factory
        self._cancel_requested: Event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None

    def scan(self) -> None:
        if self._cancel_requested.is_set():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name=f"price-{self._feed.id}",
                daemon=False,
            )
            self._thread.start()

    def close(self) -> None:
        self._cancel_requested.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()

    def _run(self) -> None:
        while not self._is_shutdown_requested():
            if self._scan_lock.acquire(timeout=0.1):
                break
        else:
            return
        try:
            if self._cancel_requested.is_set():
                return
            attempted_at = int(time.time())
            started_at = time.monotonic()
            try:
                with (
                    DeliveryStore(
                        self._dependencies.database_path,
                        initialize=False,
                    ) as store,
                    requests.Session() as session,
                ):
                    if self._feed_is_blocked(store):
                        return
                    worker_dependencies = replace(
                        self._dependencies,
                        snapshots=store,
                        sender=DiscordWebhookClient(session=session),
                        fetch_retry_policy=replace(
                            self._dependencies.fetch_retry_policy,
                            sleep=self._sleep,
                        ),
                        sqlite_retry_policy=replace(
                            self._dependencies.sqlite_retry_policy,
                            sleep=self._sleep,
                        ),
                        delivery=replace(
                            self._dependencies.delivery,
                            sleep=self._sleep,
                            is_shutdown_requested=self._is_shutdown_requested,
                        ),
                    )
                    with price_scan_timing():
                        self._factory(self._feed, worker_dependencies).scan()
                    store.record_job_timing(
                        self._feed.id,
                        "price",
                        max(0, int((time.monotonic() - started_at) * 1000)),
                        0,
                    )
            except (FeedFetchInterruptedError, SQLiteRetryInterruptedError):
                return
            except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
                self._record_failure(error, attempted_at, started_at)
        finally:
            self._scan_lock.release()

    def _feed_is_blocked(self, store: DeliveryStore) -> bool:
        now = int(time.time())
        return any(
            (store.get_blocked_until(feed_id) or 0) > now
            for feed_id in (
                self._feed.id,
                *self._dependencies.cooldown_peer_feed_ids,
            )
        )

    def _record_failure(
        self,
        error: Exception,
        attempted_at: int,
        started_at: float,
    ) -> None:
        cause = safe_error_cause(error)
        blocked = isinstance(error, FeedFetchError) and (
            error.cause_type in {"AccessChallenge", "BotChallenge"}
            or error.status_code == 403
        )
        try:
            with DeliveryStore(
                self._dependencies.database_path,
                initialize=False,
            ) as store:
                notice = store.record_health(
                    HealthUpdate(
                        feed_id=self._feed.id,
                        job_kind="price",
                        state="blocked" if blocked else "failed",
                        cause=cause,
                        attempted_at=attempted_at,
                        success=False,
                        nonempty=False,
                        item_count=0,
                        duration_ms=max(
                            0,
                            int((time.monotonic() - started_at) * 1000),
                        ),
                    ),
                )
        except sqlite3.Error as persistence_error:
            logger.log(
                logging.ERROR,
                "Background price health persistence failed for feed %s (%s; scan %s)",
                self._feed.id,
                type(persistence_error).__name__,
                cause,
            )
            return
        if notice.should_log:
            logger.error(
                "Background price scan failed for feed %s (%s)",
                self._feed.id,
                cause,
            )

    def _is_shutdown_requested(self) -> bool:
        return (
            self._cancel_requested.is_set()
            or self._dependencies.delivery.is_shutdown_requested()
        )

    def _sleep(self, seconds: float) -> bool:
        if self._is_shutdown_requested():
            return False
        return (
            not self._cancel_requested.wait(seconds)
            and not self._dependencies.delivery.is_shutdown_requested()
        )
