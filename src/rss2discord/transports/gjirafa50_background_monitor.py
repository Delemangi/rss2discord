"""Run the expensive Gjirafa50 price scan outside the scheduler thread."""

import logging
import sqlite3
from dataclasses import replace
from threading import Event, Lock, Thread
from typing import ClassVar, Final

import requests

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordWebhookClient
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
            try:
                with (
                    DeliveryStore(self._dependencies.database_path) as store,
                    requests.Session() as discord_session,
                ):
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
            except FeedFetchError as error:
                logger.exception(
                    "Gjirafa50 price scan failed for feed %s (%s)",
                    self._feed.id,
                    error.cause_type,
                )
            except sqlite3.Error as error:
                logger.exception(
                    "Gjirafa50 price persistence failed for feed %s (%s)",
                    self._feed.id,
                    type(error).__name__,
                )
            except Exception as error:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK
                logger.exception(
                    "Unexpected Gjirafa50 price scan failure for feed %s (%s)",
                    self._feed.id,
                    type(error).__name__,
                )
        finally:
            self._scan_lock.release()

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
