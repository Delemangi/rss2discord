"""Mutable absolute resource budget for one DDStore catalog operation."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import FeedFetchError

DDSTORE_LABEL: Final = "DDStore"


@dataclass(frozen=True, slots=True)
class DDStoreScanLimits:
    """Fixed elapsed-time and byte limits for one catalog operation."""

    seconds: float
    response_bytes: int


class DDStoreScanBudget:
    """Track the fixed deadline and remaining response bytes across retries."""

    __slots__ = ("bytes_remaining", "expires_at", "is_shutdown_requested", "monotonic")

    def __init__(
        self,
        limits: DDStoreScanLimits,
        monotonic: Callable[[], float],
        is_shutdown_requested: Callable[[], bool],
    ) -> None:
        self.expires_at = monotonic() + limits.seconds
        self.bytes_remaining = limits.response_bytes
        self.monotonic = monotonic
        self.is_shutdown_requested = is_shutdown_requested

    def request_timeout(self) -> tuple[float, float]:
        """Return connect/read timeouts clamped to the absolute deadline."""
        remaining_seconds = self.remaining_seconds()
        return min(5.0, remaining_seconds), remaining_seconds

    def remaining_seconds(self) -> float:
        """Return positive time remaining after shutdown and deadline checks."""
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        remaining_seconds = self.expires_at - self.monotonic()
        if remaining_seconds <= 0:
            raise FeedFetchError(DDSTORE_LABEL, "ScanTimeout")
        return remaining_seconds

    def require_byte_capacity(self, size: int) -> None:
        """Reject a declared response that exceeds the remaining scan budget."""
        self.remaining_seconds()
        if size > self.bytes_remaining:
            raise FeedFetchError(DDSTORE_LABEL, "ScanResponseTooLarge")

    def consume_bytes(self, size: int) -> None:
        """Consume streamed bytes after rechecking shutdown and deadline."""
        self.require_byte_capacity(size)
        self.bytes_remaining -= size

    def require_retry_delay(self, delay: float) -> None:
        """Reject a retry that cannot begin before this scan's deadline."""
        if delay >= self.remaining_seconds():
            raise FeedFetchError(DDSTORE_LABEL, "ScanTimeout")
