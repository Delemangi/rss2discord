import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, assert_never

import pytest

from app import RSSToDiscord
from configuration import AppConfig, FeedConfig
from delivery_store import DeliveryStore
from discord_client import SleepCallback, WebhookMessage
from models import EntryData, EntryId
from strategies import ScraperStrategy


@dataclass(frozen=True, slots=True)
class FakeEntry:
    id: EntryId
    data: EntryData


class FakeStrategy(ScraperStrategy):
    def __init__(self, entries: list[FakeEntry]) -> None:
        self.entries = entries

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        return list(self.entries), "Source"

    def get_entry_id(self, entry: Any) -> EntryId | None:  # noqa: ANN401
        return entry.id

    def get_entry_data(self, entry: Any) -> EntryData:  # noqa: ANN401
        return entry.data


class FakeSender:
    def __init__(self, outcomes: list[bool | RuntimeError]) -> None:
        self.outcomes = outcomes
        self.messages: list[WebhookMessage] = []

    def send(self, message: WebhookMessage, sleep: SleepCallback) -> bool:
        self.messages.append(message)
        outcome = self.outcomes.pop(0)
        match outcome:
            case RuntimeError():
                raise outcome
            case bool():
                return outcome
            case _ as unreachable:
                assert_never(unreachable)


def make_feed(feed_id: str) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        name=feed_id,
        url="https://example.test/feed.xml",
        webhook=f"https://discord.test/{feed_id}",
    )


def make_entry(entry_id: str) -> FakeEntry:
    return FakeEntry(
        id=EntryId(entry_id),
        data=EntryData(
            title=entry_id,
            link=f"https://example.test/{entry_id}",
            description="Description",
            author="Author",
            timestamp="2026-07-19T12:00:00+00:00",
        ),
    )


def busy_database_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def make_app(
    store: DeliveryStore,
    sender: FakeSender,
    strategy: FakeStrategy,
    feeds: tuple[FeedConfig, ...],
) -> RSSToDiscord:
    app = RSSToDiscord(
        config=AppConfig(delay_between_posts=0, max_post_age_days=0, feeds=feeds),
        store=store,
        sender=sender,
    )
    app._strategies["rss"] = strategy
    return app


def test_successful_send_is_not_repeated_when_persistence_temporarily_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([True])
    strategy = FakeStrategy([make_entry("entry-1")])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        mark_attempts = 0
        mark_delivered = store.mark_delivered
        retry_delays: list[float] = []

        def record_persistence_sleep(seconds: float) -> bool:
            retry_delays.append(seconds)
            return True

        def temporarily_locked(feed_id: str, entry_id: str) -> None:
            nonlocal mark_attempts
            mark_attempts += 1
            if mark_attempts == 1:
                raise busy_database_error()
            mark_delivered(feed_id, entry_id)

        monkeypatch.setattr(store, "mark_delivered", temporarily_locked)
        monkeypatch.setattr(
            app,
            "_interruptible_sleep",
            record_persistence_sleep,
        )

        # When
        app.process_feed(feed)

        # Then
        assert store.has_delivered("news", "entry-1")
        assert mark_attempts == 2
        assert retry_delays[0] == pytest.approx(5.0)
        assert len(sender.messages) == 1


def test_shutdown_interrupts_persistence_retry_after_immediate_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([True])
    strategy = FakeStrategy([make_entry("entry-1")])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        mark_attempts = 0
        mark_delivered = store.mark_delivered

        def temporarily_locked(feed_id: str, entry_id: str) -> None:
            nonlocal mark_attempts
            mark_attempts += 1
            if mark_attempts == 1:
                raise busy_database_error()
            mark_delivered(feed_id, entry_id)

        def request_shutdown(_seconds: float) -> bool:
            app.request_shutdown()
            return False

        monkeypatch.setattr(store, "mark_delivered", temporarily_locked)
        monkeypatch.setattr(app, "_interruptible_sleep", request_shutdown)

        # When
        app.process_feed(feed)

        # Then
        assert not store.has_delivered("news", "entry-1")
        assert mark_attempts == 1
        assert len(sender.messages) == 1


def test_persistence_attempts_once_when_shutdown_is_already_requested(
    tmp_path: Path,
) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([])
    strategy = FakeStrategy([])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        app.request_shutdown()

        # When
        persisted = app._persist_delivery("news", EntryId("entry-1"))

        # Then
        assert persisted
        assert store.has_delivered("news", "entry-1")


def test_permanent_persistence_error_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([])
    strategy = FakeStrategy([])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        mark_attempts = 0
        mark_delivered = store.mark_delivered
        retry_delays: list[float] = []

        def corrupt_then_succeed(feed_id: str, entry_id: str) -> None:
            nonlocal mark_attempts
            mark_attempts += 1
            if mark_attempts == 1:
                error = sqlite3.DatabaseError("database disk image is malformed")
                error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
                raise error
            mark_delivered(feed_id, entry_id)

        def record_sleep(seconds: float) -> bool:
            retry_delays.append(seconds)
            return True

        monkeypatch.setattr(store, "mark_delivered", corrupt_then_succeed)
        monkeypatch.setattr(app, "_interruptible_sleep", record_sleep)

        # When / Then
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            app._persist_delivery("news", EntryId("entry-1"))
        assert mark_attempts == 1
        assert retry_delays == []
