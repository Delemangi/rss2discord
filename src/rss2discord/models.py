from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, NewType

EntryId = NewType("EntryId", str)

MAX_INLINE_PRICE_CHANGES: Final = 25
HEALTH_REMINDER_SECONDS: Final = 21_600


class PriceDirection(StrEnum):
    """Which way a monitored price moved, when the mover is known.

    Left unset when the comparison is not a straight up/down, such as a change
    that crossed currencies.
    """

    DECREASE = "decrease"
    INCREASE = "increase"


@dataclass(frozen=True, slots=True)
class SourceMetric:
    label: str
    value: str
    # Marks the figure the headline metric replaced, such as the price a drop
    # alert moved away from. It is rendered struck through beside the headline
    # instead of being demoted into the supporting line.
    prior: bool = False


@dataclass(frozen=True, slots=True)
class EntryData:
    title: str
    link: str
    description: str
    author: str
    timestamp: str | None
    discussion_url: str | None = None
    image_url: str | None = None
    categories: tuple[str, ...] = ()
    source_metrics: tuple[SourceMetric, ...] = ()
    price_direction: PriceDirection | None = None


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
