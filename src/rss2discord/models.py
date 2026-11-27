from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, NewType

from rss2discord.defaults import PRICE_HISTORY_WINDOW_DAYS

EntryId = NewType("EntryId", str)

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
class PriceHistoryWindow:
    """Low and high observations for one price-history window."""

    days: int | None
    label: str
    low: str
    high: str
    is_low: bool


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
    # Source-assigned popularity score, when the adapter can provide one.
    source_score: int | None = None
    price_direction: PriceDirection | None = None
    # Ordered media gallery URLs. When non-empty, renderers prefer this over
    # the single image_url thumbnail.
    image_urls: tuple[str, ...] = ()
    price_change_percentage: str | None = None
    discount_percentage: str | None = None
    # Price-history context for alerts. Both values are formatted in the
    # source's own format and cover the window ending at this observation.
    price_period_low: str | None = None
    price_period_high: str | None = None
    price_period_days: int = PRICE_HISTORY_WINDOW_DAYS
    price_is_period_low: bool = False
    # Multi-window history used by current renderers; the scalar fields above
    # remain for outbox compatibility and older pending rows.
    price_history: tuple[PriceHistoryWindow, ...] = ()
    # Marks a stock alert instead of a price move, so renderers do not title
    # the card as a change and digests can count it separately.
    restocked: bool = False


@dataclass(frozen=True, slots=True)
class PriceHistoryContext:
    """Observed price range for one product over one or more windows."""

    low: Decimal
    low_formatted: str
    high: Decimal
    high_formatted: str
    is_low: bool
    windows: tuple[PriceHistoryWindow, ...] = ()


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """One source-neutral price observation."""

    feed_id: str
    product_id: str
    amount: Decimal
    formatted: str
    currency: str
    # Stock state when the source exposes it. ``None`` means the source did not
    # report availability, which is never treated as a restock.
    available: bool | None = None


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
