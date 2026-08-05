"""Run the expensive Gjirafa50 price scan outside the scheduler thread."""

import logging
import sqlite3
from dataclasses import replace
from threading import Lock, Thread
from typing import ClassVar

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
        with self._scan_lock:
            try:
                with (
                    DeliveryStore(self._dependencies.database_path) as store,
                    requests.Session() as discord_session,
                ):
                    dependencies = replace(
                        self._dependencies,
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

    def close(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join()
