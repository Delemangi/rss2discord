"""Contracts stage for source-neutral price monitoring."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from rss2discord.discord.client import DiscordSender, SleepCallback
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationRecord,
)
from rss2discord.price_safety import HEALTH_REMINDER_SECONDS
from rss2discord.recovery_models import (
    HealthNotice,
    HealthUpdate,
    PriceSnapshot,
)
from rss2discord.retries import SQLiteRetryPolicy


class PriceSnapshotStore(Protocol):
    """Persist source-neutral price snapshots for one feed."""

    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]: ...

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None: ...

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None: ...


class PriceRecoveryStore(PriceSnapshotStore, Protocol):
    def record_price_scan(
        self,
        *,
        feed_id: str,
        group_id: str,
        observed_at: int,
        baseline_updates: Iterable[PriceSnapshot],
        notifications: Iterable[PriceNotification],
    ) -> int: ...

    def load_pending_price_group(
        self,
        feed_id: str,
    ) -> tuple[str, tuple[PriceNotificationRecord, ...]] | None: ...

    def mark_price_notifications_sent(
        self,
        notification_ids: Iterable[int],
    ) -> None: ...

    def record_price_notification_attempt(
        self,
        notification_ids: Iterable[int],
        error: str | None,
    ) -> None: ...

    def count_pending_price_notifications(self, feed_id: str) -> int: ...

    def select_price_confirmation_subset(
        self,
        *,
        feed_id: str,
        product_ids: Iterable[str],
        limit: int,
    ) -> tuple[str, ...]: ...

    def record_health(
        self,
        update: HealthUpdate,
        *,
        reminder_seconds: int = HEALTH_REMINDER_SECONDS,
    ) -> HealthNotice: ...

    def list_price_notifications(
        self,
        feed_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]: ...


@dataclass(frozen=True, slots=True)
class PriceDeliveryPlan:
    selected_ids: tuple[str, ...] = ()
    bulk: bool = False
    additional_pending: int = 0
    item_count: int = 0


class PriceDeliveryOutcome(StrEnum):
    """Observable outcome of delivering one planned price change set."""

    DELIVERED = "delivered"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class PriceAlertDelivery:
    """Control sequential Discord delivery and observe runtime shutdown state."""

    sleep: SleepCallback
    delay_between_posts: float
    is_shutdown_requested: Callable[[], bool]


class DeliverablePriceChange(Protocol):
    @property
    def previous(self) -> PriceSnapshot: ...

    @property
    def current(self) -> PriceSnapshot: ...


class PriceChangeDeliveryDependencies(Protocol):
    @property
    def snapshots(self) -> PriceRecoveryStore: ...

    @property
    def sender(self) -> DiscordSender: ...

    @property
    def sqlite_retry_policy(self) -> SQLiteRetryPolicy: ...

    @property
    def delivery(self) -> PriceAlertDelivery: ...
