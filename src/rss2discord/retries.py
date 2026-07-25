"""Interruptible retry policies for fetch and SQLite operations."""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, TypeVar

from .fetch_errors import FeedFetchError

FETCH_MAX_ATTEMPTS: Final = 3
FETCH_BASE_DELAY_SECONDS: Final = 2.0
FETCH_MAX_DELAY_SECONDS: Final = 300.0
FETCH_MAX_BACKOFF_SECONDS: Final = 30.0
SQLITE_RETRY_DELAY_SECONDS: Final = 5.0
SQLITE_TRANSIENT_ERROR_CODES: Final = frozenset(
    {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED},
)

T = TypeVar("T")


class RetrySleep(Protocol):
    """Wait until a retry while reporting whether shutdown was requested."""

    def __call__(self, seconds: float) -> bool: ...


class FetchRetryLogger(Protocol):
    """Report a retry using caller-owned, sanitized context."""

    def __call__(self, error: FeedFetchError, delay: float) -> None: ...


class SQLiteRetryLogger(Protocol):
    """Report a SQLite retry using caller-owned, sanitized context."""

    def __call__(self, error: sqlite3.Error, delay: float) -> None: ...


class FeedFetchInterruptedError(Exception):
    """Raised when shutdown interrupts fetch retry backoff."""


class SQLiteRetryInterruptedError(Exception):
    """Raised when shutdown interrupts a transient SQLite retry."""


@dataclass(frozen=True, slots=True)
class FetchRetryPolicy:
    """Retry retryable feed operations with bounded, interruptible backoff."""

    sleep: RetrySleep
    on_retry: FetchRetryLogger

    def execute(self, operation: Callable[[], T]) -> T:
        """Run an operation up to three times for retryable fetch errors."""
        for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
            try:
                return operation()
            except FeedFetchError as error:
                if not error.retryable or attempt == FETCH_MAX_ATTEMPTS:
                    raise
                delay = _fetch_retry_delay(error, attempt - 1)
                self.on_retry(error, delay)
                if not self.sleep(delay):
                    raise FeedFetchInterruptedError from None
        raise AssertionError


@dataclass(frozen=True, slots=True)
class SQLiteRetryPolicy:
    """Retry busy or locked SQLite operations until completion or shutdown."""

    sleep: RetrySleep
    on_retry: SQLiteRetryLogger

    def execute(self, operation: Callable[[], T]) -> T:
        """Run an operation until a transient SQLite failure clears or shutdown occurs."""
        while True:
            try:
                return operation()
            except sqlite3.Error as error:
                if not _is_transient_sqlite_error(error):
                    raise
                self.on_retry(error, SQLITE_RETRY_DELAY_SECONDS)
                if not self.sleep(SQLITE_RETRY_DELAY_SECONDS):
                    raise SQLiteRetryInterruptedError from None


def _fetch_retry_delay(error: FeedFetchError, attempt: int) -> float:
    if error.retry_after is not None:
        return min(error.retry_after, FETCH_MAX_DELAY_SECONDS)
    backoff = min(
        FETCH_BASE_DELAY_SECONDS * (2**attempt),
        FETCH_MAX_BACKOFF_SECONDS,
    )
    return random.SystemRandom().uniform(0, backoff)


def _is_transient_sqlite_error(error: sqlite3.Error) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    return (
        isinstance(error_code, int)
        and error_code & 0xFF in SQLITE_TRANSIENT_ERROR_CODES
    )
