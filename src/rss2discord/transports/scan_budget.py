"""Shared mutable budget for one bounded multi-request catalog scan."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.retries import FeedFetchInterruptedError


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Fixed limits for one provider catalog scan."""

    label: str
    max_seconds: float
    max_requests: int
    max_bytes: int


@dataclass(slots=True)
class ScanBudget:
    """Track elapsed time, request count, and response bytes for one scan."""

    limits: ScanLimits
    is_shutdown_requested: Callable[[], bool]
    started_at: float
    requests: int = 0
    response_bytes: int = 0

    @classmethod
    def start(
        cls,
        limits: ScanLimits,
        is_shutdown_requested: Callable[[], bool],
    ) -> ScanBudget:
        return cls(
            limits=limits,
            is_shutdown_requested=is_shutdown_requested,
            started_at=monotonic(),
        )

    def before_request(self) -> None:
        self._check()
        self.requests += 1
        if self.requests > self.limits.max_requests:
            raise FeedFetchError(self.limits.label, "RequestLimitExceeded")

    def request_timeout(self) -> float:
        remaining = self.limits.max_seconds - (monotonic() - self.started_at)
        if remaining <= 0:
            raise FeedFetchError(self.limits.label, "ScanTimeLimitExceeded")
        return remaining

    def require_retry_delay(self, delay: float) -> None:
        if monotonic() - self.started_at + delay > self.limits.max_seconds:
            raise FeedFetchError(self.limits.label, "ScanTimeLimitExceeded")

    def add_bytes(self, amount: int) -> None:
        self.response_bytes += amount
        if self.response_bytes > self.limits.max_bytes:
            raise FeedFetchError(self.limits.label, "ScanResponseTooLarge")
        self._check()

    def before_chunk(self) -> None:
        self._check()

    def after_request(self) -> None:
        self.before_chunk()

    def _check(self) -> None:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        if monotonic() - self.started_at >= self.limits.max_seconds:
            raise FeedFetchError(self.limits.label, "ScanTimeLimitExceeded")
