"""Shared test doubles for provider HTTP and Discord delivery."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import requests

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    DiscordSender,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.models import PriceSnapshot
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
    PriceRecoveryStore,
)


@dataclass
class StubResponse:
    """Response surface shared by requests and curl-style provider fakes."""

    content: bytes = b""
    status_code: int = 200
    headers: MutableMapping[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None
    url: str = ""
    interruption: BaseException | None = None
    raw_header_lines: tuple[bytes, ...] | None = None
    on_chunk: Callable[[], None] | None = None
    text: str | None = None

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400 and "Location" in self.headers

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        if self.chunks is not None:
            yield from self.chunks
        elif self.text is not None:
            yield self.text.encode()
        else:
            yield self.content
        if self.interruption is not None:
            raise self.interruption

    def __enter__(self) -> StubResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def close(self) -> None:
        return None


class RecordingSender:
    """Record outgoing messages and replay queued delivery outcomes."""

    def __init__(
        self,
        outcomes: Sequence[bool | DiscordDeliveryResult],
    ) -> None:
        self._outcomes = list(outcomes)
        self.messages: list[WebhookMessage] = []

    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        del sleep
        self.messages.append(message)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, bool):
            return (
                DiscordDeliveryResult.DELIVERED
                if outcome
                else DiscordDeliveryResult.FAILED
            )
        return outcome


class RetrySleepAdapter:
    """Adapt a retry callback to the sleep protocol used by provider tests."""

    def __init__(self, sleep: SleepCallback) -> None:
        self._sleep = sleep

    def __call__(self, seconds: float) -> bool:
        return self._sleep(seconds)


def keep_running(_seconds: float) -> bool:
    return True


def is_not_shutdown() -> bool:
    return False


def busy_database_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def make_price_monitor_dependencies[CatalogT](
    catalog: CatalogT,
    snapshots: PriceRecoveryStore,
    sender: DiscordSender,
    *,
    sleep: SleepCallback = keep_running,
    delay_between_posts: float = 0,
    is_shutdown_requested: Callable[[], bool] = is_not_shutdown,
) -> PriceMonitorDependencies[CatalogT]:
    """Build source-neutral price-monitor dependencies with test doubles."""
    retry_sleep = RetrySleepAdapter(sleep)
    return PriceMonitorDependencies(
        catalog=catalog,
        snapshots=snapshots,
        sender=sender,
        fetch_retry_policy=FetchRetryPolicy(
            sleep=retry_sleep,
            on_retry=lambda error, delay: None,
        ),
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=retry_sleep,
            on_retry=lambda error, delay: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=sleep,
            delay_between_posts=delay_between_posts,
            is_shutdown_requested=is_shutdown_requested,
        ),
    )


class SnapshotStoreSpy(DeliveryStore):
    """Record baseline loads and persistence without returning snapshots."""

    def __init__(self) -> None:
        super().__init__(Path(":memory:"))
        self.load_calls = 0
        self.persisted_batches: list[tuple[PriceSnapshot, ...]] = []

    def load_price_snapshots(
        self,
        feed_id: str,
        *,
        limit: int | None = None,
    ) -> tuple[PriceSnapshot, ...]:
        del feed_id, limit
        self.load_calls += 1
        return ()

    def upsert_price_snapshot(self, snapshot: PriceSnapshot) -> None:
        self.persisted_batches.append((snapshot,))

    def upsert_price_snapshots(self, snapshots: Iterable[PriceSnapshot]) -> None:
        self.persisted_batches.append(tuple(snapshots))
