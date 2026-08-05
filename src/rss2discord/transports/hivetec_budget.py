"""Absolute elapsed-time and response-byte budget for one Hivetec operation."""

import math
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.hivetec_bounds import HIVETEC_LABEL


@dataclass(frozen=True, slots=True)
class HivetecScanLimits:
    seconds: float
    response_bytes: int


class HivetecScanBudget:
    """Track one fixed deadline and byte allowance across requests and retries."""

    __slots__ = ("bytes_remaining", "clock", "expires_at", "is_shutdown_requested")

    def __init__(
        self,
        limits: HivetecScanLimits,
        clock: Callable[[], float],
        is_shutdown_requested: Callable[[], bool],
    ) -> None:
        self.expires_at = clock() + limits.seconds
        self.bytes_remaining = limits.response_bytes
        self.clock = clock
        self.is_shutdown_requested = is_shutdown_requested

    @classmethod
    def start(
        cls,
        limits: HivetecScanLimits,
        is_shutdown_requested: Callable[[], bool],
    ) -> "HivetecScanBudget":
        return cls(limits, monotonic, is_shutdown_requested)

    def transfer_timeout_ms(self) -> int:
        """Return a positive libcurl total timeout inside the fixed deadline."""
        return max(1, min(math.ceil(self.remaining_seconds() * 1000), 30_000))

    def remaining_seconds(self) -> float:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        remaining = self.expires_at - self.clock()
        if remaining <= 0:
            raise FeedFetchError(HIVETEC_LABEL, "ScanTimeout")
        return remaining

    def require_byte_capacity(self, size: int) -> None:
        self.remaining_seconds()
        if size > self.bytes_remaining:
            raise FeedFetchError(HIVETEC_LABEL, "ScanResponseTooLarge")

    def consume_bytes(self, size: int) -> None:
        self.require_byte_capacity(size)
        self.bytes_remaining -= size

    def require_retry_delay(self, delay: float) -> None:
        if delay >= self.remaining_seconds():
            raise FeedFetchError(HIVETEC_LABEL, "ScanTimeout")
