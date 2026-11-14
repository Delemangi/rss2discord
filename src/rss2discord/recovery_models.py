"""Small immutable value objects shared by runtime and price lanes."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """One source-neutral price observation."""

    feed_id: str
    product_id: str
    amount: Decimal
    formatted: str
    currency: str


@dataclass(frozen=True, slots=True)
class PriceChangeRecord:
    """A stable-ID price transition from a persisted to a current snapshot."""

    product_id: str
    previous: PriceSnapshot
    current: PriceSnapshot


@dataclass(frozen=True, slots=True)
class HealthUpdate:
    """One completed ordinary or price job observation."""

    feed_id: str
    job_kind: str
    state: str
    cause: str | None
    attempted_at: int
    success: bool
    nonempty: bool
    item_count: int
    duration_ms: int | None = None
    scheduler_lag_ms: int | None = None


@dataclass(frozen=True, slots=True)
class HealthNotice:
    """Whether a health observation should be surfaced to operators."""

    changed: bool
    recovered: bool
    should_log: bool
    previous_state: str | None


@dataclass(frozen=True, slots=True)
class HealthRecord:
    """Persisted health state returned by administrative inspection."""

    feed_id: str
    job_kind: str
    state: str
    cause: str | None
    attempted_at: int | None
    success: bool
    nonempty: bool
    item_count: int
    duration_ms: int | None
    scheduler_lag_ms: int | None
    transition_at: int | None
    consecutive_failures: int
    total_attempts: int
    total_successes: int
    total_nonempty: int
    total_items: int
    blocked_until: int | None
    last_notified_at: int | None
    last_success_at: int | None = None
    last_nonempty_at: int | None = None
