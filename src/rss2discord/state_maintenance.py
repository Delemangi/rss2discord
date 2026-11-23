"""Periodic state retention for long-running processes."""

import logging
import sqlite3
from pathlib import Path
from threading import Event, Thread
from typing import Final

from rss2discord.delivery_store import DeliveryStore

logger = logging.getLogger(__name__)

DEFAULT_STATE_RETENTION_INTERVAL_SECONDS: Final = 86_400.0


class StateRetentionReporter:
    """Prune retained rows daily without the startup-only VACUUM."""

    def __init__(
        self,
        *,
        database_path: Path,
        retention_days: int,
        interval: float = DEFAULT_STATE_RETENTION_INTERVAL_SECONDS,
    ) -> None:
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        if interval <= 0:
            raise ValueError("retention interval must be positive")
        self._database_path = database_path
        self._retention_days = retention_days
        self._interval = interval
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._retention_days <= 0:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(
            target=self._run,
            name="state-retention",
            daemon=False,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def prune_now(self) -> int:
        if self._retention_days <= 0:
            return 0
        try:
            with DeliveryStore(self._database_path, initialize=False) as store:
                result = store.prune_old_records(
                    retention_days=self._retention_days,
                )
        except sqlite3.Error as error:
            logger.log(
                logging.ERROR,
                "Periodic state retention failed (%s)",
                type(error).__name__,
            )
            return 0
        if result.total:
            logger.info(
                "Periodic state retention removed %d rows "
                "(delivered=%d notifications=%d history=%d)",
                result.total,
                result.delivered_entries,
                result.price_notifications,
                result.price_history,
            )
        return result.total

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.prune_now()
