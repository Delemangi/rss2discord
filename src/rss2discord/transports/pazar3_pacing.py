from collections.abc import Callable
from typing import Final

from rss2discord.discord.client import SleepCallback
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.pazar3_scope import PAZAR3_LABEL

PAZAR3_REQUEST_INTERVAL_SECONDS: Final = 20.0


class Pazar3RequestPacer:
    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._next_request_at: float | None = None

    def wait(
        self,
        sleep: SleepCallback,
        is_shutdown_requested: Callable[[], bool],
        deadline: float | None = None,
    ) -> None:
        if is_shutdown_requested():
            raise FeedFetchInterruptedError
        now = self._monotonic()
        if deadline is not None and now >= deadline:
            raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded")
        if self._next_request_at is not None and now < self._next_request_at:
            if deadline is not None and self._next_request_at >= deadline:
                raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded")
            if not sleep(self._next_request_at - now):
                raise FeedFetchInterruptedError
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if deadline is not None and self._monotonic() >= deadline:
                raise FeedFetchError(PAZAR3_LABEL, "ScanTimeLimitExceeded")
        if is_shutdown_requested():
            raise FeedFetchInterruptedError
        self._next_request_at = self._monotonic() + PAZAR3_REQUEST_INTERVAL_SECONDS
