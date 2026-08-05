"""Source-neutral contracts for sequential catalog price monitors."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Protocol, assert_never

from rss2discord.delivery_store import PriceSnapshot
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    DiscordSender,
    SleepCallback,
)
from rss2discord.discord.message import WebhookMessage
from rss2discord.retries import SQLiteRetryPolicy


class PriceSnapshotStore(Protocol):
    """Persist source-neutral price snapshots for one feed."""

    def load_price_snapshots(self, feed_id: str) -> tuple[PriceSnapshot, ...]: ...

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None: ...

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None: ...


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
    def snapshots(self) -> PriceSnapshotStore: ...

    @property
    def sender(self) -> DiscordSender: ...

    @property
    def sqlite_retry_policy(self) -> SQLiteRetryPolicy: ...

    @property
    def delivery(self) -> PriceAlertDelivery: ...


def deliver_price_changes[PriceChangeT: DeliverablePriceChange](
    changes: Iterable[PriceChangeT],
    dependencies: PriceChangeDeliveryDependencies,
    message_for: Callable[[PriceChangeT], WebhookMessage],
) -> None:
    delay_before_next = False
    for change in changes:
        if dependencies.delivery.is_shutdown_requested():
            return
        if (
            delay_before_next
            and dependencies.delivery.delay_between_posts > 0
            and not dependencies.delivery.sleep(
                dependencies.delivery.delay_between_posts,
            )
        ):
            return
        delay_before_next = False
        result = dependencies.sender.send(
            message_for(change),
            dependencies.delivery.sleep,
        )
        match result:
            case DiscordDeliveryResult.DELIVERED:
                dependencies.sqlite_retry_policy.execute(
                    partial(
                        dependencies.snapshots.upsert_price_snapshot,
                        change.current,
                    ),
                )
                delay_before_next = True
            case DiscordDeliveryResult.FAILED:
                if dependencies.delivery.is_shutdown_requested():
                    return
            case DiscordDeliveryResult.INTERRUPTED:
                return
            case unreachable:
                assert_never(unreachable)
