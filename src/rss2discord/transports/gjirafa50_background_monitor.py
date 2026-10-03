"""Run the expensive Gjirafa50 price scan outside the scheduler thread."""

import logging
import re
import sqlite3
import time
from dataclasses import replace
from threading import Event, Lock, Thread
from typing import ClassVar, Final

import requests

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordWebhookClient
from rss2discord.recovery_models import HealthUpdate
from rss2discord.retries import FeedFetchInterruptedError, SQLiteRetryInterruptedError
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.gjirafa50_price_monitor import (
    Gjirafa50PriceMonitor,
    Gjirafa50PriceMonitorDependencies,
)

logger = logging.getLogger(__name__)
GJIRAFA50_SCAN_LOCK_POLL_SECONDS: Final = 0.1


class Gjirafa50BackgroundPriceMonitor:
    """Serialize provider scans while isolating each feed's worker resources."""

    _scan_lock: ClassVar[Lock] = Lock()

    def __init__(
        self,
        feed: FeedConfig,
        dependencies: Gjirafa50PriceMonitorDependencies,
    ) -> None:
        self._feed = feed
        self._dependencies = dependencies
        self._cancel_requested: Event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None

    def scan(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = Thread(
                target=self._run,
                name=f"gjirafa50-price-{self._feed.id}",
                daemon=False,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._is_shutdown_requested():
            if self._scan_lock.acquire(timeout=GJIRAFA50_SCAN_LOCK_POLL_SECONDS):
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
                    DeliveryStore(self._dependencies.database_path) as store,
                    requests.Session() as discord_session,
                ):
                    if any(
                        (store.get_blocked_until(feed_id) or 0) > int(time.time())
                        for feed_id in (
                            self._feed.id,
                            *self._dependencies.cooldown_peer_feed_ids,
                        )
                    ):
                        return
                    dependencies = replace(
                        self._dependencies,
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
                        snapshots=store,
                        sender=DiscordWebhookClient(session=discord_session),
                    )
                    Gjirafa50PriceMonitor(self._feed, dependencies).scan()
            except (FeedFetchInterruptedError, SQLiteRetryInterruptedError):
                return
            except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
                self._record_failure(error, attempted_at, started_at)
        finally:
            self._scan_lock.release()

    def _record_failure(
        self,
        error: Exception,
        attempted_at: int,
        started_at: float,
    ) -> None:
        cause = (
            error.cause_type
            if isinstance(error, FeedFetchError)
            else type(error).__name__
        )
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", cause):
            cause = "FetchError"
        blocked = isinstance(error, FeedFetchError) and (
            error.cause_type in {"AccessChallenge", "BotChallenge"}
            or error.status_code == 403
        )
        try:
            # The worker owns this connection too; never use the scheduler's
            # connection across threads, including on failure paths.
            with DeliveryStore(self._dependencies.database_path) as store:
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
                        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                    ),
                )
        except sqlite3.Error as persistence_error:
            logger.error(  # noqa: TRY400 - exception payloads may contain credentials
                "Gjirafa50 health persistence failed for feed %s (%s; scan %s)",
                self._feed.id,
                type(persistence_error).__name__,
                cause,
            )
            return
        if notice.should_log:
            logger.error(
                "Gjirafa50 price scan failed for feed %s (%s)",
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

    def close(self) -> None:
        self._cancel_requested.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()
