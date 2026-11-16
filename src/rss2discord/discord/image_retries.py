"""Bounded retry control for product thumbnail transfers."""

import math
import time
from collections.abc import Callable
from typing import Final, final

from curl_cffi import requests as curl_requests

from rss2discord.curl_errors import TRANSIENT_CURL_ERRORS
from rss2discord.retries import parse_retry_after

__all__ = (
    "ImageDownloadInterruptedError",
    "ImageRetryBudget",
    "RetrySleep",
    "is_retryable_image_request_error",
)

MAX_IMAGE_DOWNLOAD_SECONDS: Final = 30.0
MAX_IMAGE_TRANSFER_MS: Final = 30_000
MAX_IMAGE_RETRIES: Final = 2
IMAGE_RETRY_BASE_DELAY_SECONDS: Final = 1.0
type RetrySleep = Callable[[float], bool]


class ImageDownloadInterruptedError(Exception):
    """Raised when shutdown interrupts a thumbnail retry wait."""


def is_retryable_image_request_error(error: curl_requests.RequestsError) -> bool:
    """Return whether curl reported a transient transport failure."""
    return error.code in TRANSIENT_CURL_ERRORS


@final
class ImageRetryBudget:
    """Mutate one download's fixed deadline and remaining retry allowance."""

    __slots__ = ("_expires_at", "_monotonic", "_retries_remaining", "_sleep")

    def __init__(
        self,
        sleep: RetrySleep | None,
        monotonic_clock: Callable[[], float],
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic_clock
        self._expires_at = monotonic_clock() + MAX_IMAGE_DOWNLOAD_SECONDS
        self._retries_remaining = MAX_IMAGE_RETRIES

    def transfer_timeout_ms(self) -> int | None:
        """Return a positive transfer timeout within the fixed deadline."""
        remaining_seconds = self._expires_at - self._monotonic()
        if remaining_seconds <= 0:
            return None
        return max(
            1,
            min(math.ceil(remaining_seconds * 1000), MAX_IMAGE_TRANSFER_MS),
        )

    def has_time_remaining(self) -> bool:
        """Return whether the fixed operation deadline has not elapsed."""
        return self._monotonic() < self._expires_at

    def wait_for_retry(self, retry_after: str | None = None) -> bool:
        """Consume one retry and wait only when the deadline permits it."""
        if self._retries_remaining == 0:
            return False
        retry_number = MAX_IMAGE_RETRIES - self._retries_remaining
        parsed_retry_after = parse_retry_after(retry_after)
        delay = (
            parsed_retry_after
            if parsed_retry_after is not None
            else IMAGE_RETRY_BASE_DELAY_SECONDS * (2**retry_number)
        )
        if delay >= self._expires_at - self._monotonic():
            return False
        self._retries_remaining -= 1
        if self._sleep is not None:
            if not self._sleep(delay):
                raise ImageDownloadInterruptedError
            return True
        time.sleep(delay)
        return True
