"""Small immutable value objects shared by recovery and runtime lanes."""

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
class PriceBatchItem:
    """Persisted manifest item, including delivery-attempt bookkeeping."""

    product_id: str
    previous: PriceSnapshot
    current: PriceSnapshot
    status: str = "pending"
    attempt_count: int = 0
    last_attempt_at: int | None = None
    ordinal: int = 0
    delivered_at: int | None = None


@dataclass(frozen=True, slots=True)
class PriceBatchSummary:
    """Bounded audit summary for a price-change manifest."""

    batch_id: int
    feed_id: str
    provider: str
    fingerprint: str
    status: str
    catalog_count: int
    available_count: int
    item_count: int
    pending_count: int
    delivered_count: int
    created_at: int
    reason: str | None = None
    approved_at: int | None = None
    paused_at: int | None = None
    completed_at: int | None = None


@dataclass(frozen=True, slots=True)
class PriceBatch:
    """A price-change batch and its immutable manifest items."""

    summary: PriceBatchSummary
    items: tuple[PriceBatchItem, ...] = ()

    @property
    def batch_id(self) -> int:
        return self.summary.batch_id

    @property
    def feed_id(self) -> str:
        return self.summary.feed_id

    @property
    def provider(self) -> str:
        return self.summary.provider

    @property
    def fingerprint(self) -> str:
        return self.summary.fingerprint

    @property
    def status(self) -> str:
        return self.summary.status

    @property
    def item_count(self) -> int:
        return self.summary.item_count

    @property
    def pending_count(self) -> int:
        return self.summary.pending_count

    @property
    def delivered_count(self) -> int:
        return self.summary.delivered_count


@dataclass(frozen=True, slots=True)
class BaselineCandidateSummary:
    """A complete, approval-gated ordinary-feed baseline candidate."""

    feed_id: str
    fingerprint: str
    entry_ids: tuple[str, ...]
    status: str = "candidate"
    reason: str | None = None
    created_at: int | None = None
    approved_at: int | None = None


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
