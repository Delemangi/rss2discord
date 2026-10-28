"""Contracts stage for source-neutral price monitoring."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from rss2discord.discord.client import DiscordSender, SleepCallback
from rss2discord.price_safety import (
    HEALTH_REMINDER_SECONDS,
    MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
)
from rss2discord.recovery_models import (
    HealthNotice,
    HealthUpdate,
    PriceBatch,
    PriceChangeRecord,
    PriceDeliveryClaim,
    PriceSnapshot,
)
from rss2discord.retries import SQLiteRetryPolicy


class PriceSnapshotStore(Protocol):
    """Persist source-neutral price snapshots for one feed."""

    def load_price_snapshots(self, feed_id: str) -> tuple[PriceSnapshot, ...]: ...

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None: ...

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None: ...


class PriceRecoveryStore(PriceSnapshotStore, Protocol):
    def held_price_product_ids(self, feed_id: str) -> frozenset[str]: ...

    def record_price_change_candidate(
        self,
        *,
        feed_id: str,
        provider: str,
        fingerprint: str,
        catalog_count: int,
        available_count: int,
        items: Iterable[PriceChangeRecord],
    ) -> PriceBatch: ...

    def load_active_price_batch(self, feed_id: str) -> PriceBatch | None: ...

    def pause_price_change_batch(self, batch_id: int, reason: str) -> PriceBatch: ...

    def claim_price_delivery_attempt(
        self,
        batch_id: int,
        product_id: str,
    ) -> PriceDeliveryClaim | None: ...

    def release_price_delivery_attempt(self, claim: PriceDeliveryClaim) -> bool: ...

    def select_normal_price_deliveries(
        self,
        *,
        feed_id: str,
        product_ids: Iterable[str],
        limit: int = MAX_PRICE_DELIVERY_ATTEMPTS_PER_SCAN,
    ) -> tuple[str, ...] | None: ...

    def record_approved_price_delivery(
        self,
        claim: PriceDeliveryClaim,
        snapshot: PriceSnapshot,
    ) -> None: ...

    def complete_price_change_batch_if_drained(self, batch_id: int) -> bool: ...

    def record_health(
        self,
        update: HealthUpdate,
        *,
        reminder_seconds: int = HEALTH_REMINDER_SECONDS,
    ) -> HealthNotice: ...


@dataclass(frozen=True, slots=True)
class PriceDeliveryPlan:
    selected_ids: tuple[str, ...] = ()
    batch_id: int | None = None
    blocked: bool = False

    @property
    def allow_silent_updates(self) -> bool:
        return not self.blocked and self.batch_id is None


@dataclass(frozen=True, slots=True)
class PriceAlertDelivery:
    """Control sequential Discord delivery and observe runtime shutdown state."""

    sleep: SleepCallback
    delay_between_posts: float
    is_shutdown_requested: Callable[[], bool]


class DeliverablePriceChange(Protocol):
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


class CatalogPriceChange(DeliverablePriceChange, Protocol):
    @property
    def previous(self) -> PriceSnapshot: ...
