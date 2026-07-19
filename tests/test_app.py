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


def test_same_url_delivers_independently_for_each_feed_id(tmp_path: Path) -> None:
    # Given
    primary = make_feed("primary")
    secondary = make_feed("secondary")
    sender = FakeSender([True, True])
    strategy = FakeStrategy([make_entry("entry-1")])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (primary, secondary))
        app.process_feed(primary)
        app.process_feed(secondary)

        # Then
        assert store.has_delivered("primary", "entry-1")
        assert store.has_delivered("secondary", "entry-1")
    assert [message.feed.id for message in sender.messages] == [
        "primary",
        "secondary",
    ]


def test_failed_delivery_is_retried_and_only_success_is_recorded(
    tmp_path: Path,
) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([False, True])
    strategy = FakeStrategy([make_entry("entry-1")])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        app.process_feed(feed)
        first_attempt_delivered = store.has_delivered("news", "entry-1")
        app.process_feed(feed)

        # Then
        assert not first_attempt_delivered
        assert store.has_delivered("news", "entry-1")
    assert len(sender.messages) == 2


def test_duplicate_entries_in_one_fetch_are_sent_once(tmp_path: Path) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([True])
    strategy = FakeStrategy([make_entry("entry-1"), make_entry("entry-1")])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        app.process_feed(feed)

    # Then
    assert len(sender.messages) == 1


def test_success_is_committed_before_later_delivery_crashes(tmp_path: Path) -> None:
    # Given
    feed = make_feed("news")
    sender = FakeSender([True, RuntimeError("transport crashed")])
    strategy = FakeStrategy([make_entry("entry-1"), make_entry("entry-2")])
    database_path = tmp_path / "state.db"

    # When
    with DeliveryStore(database_path) as store:
        app = make_app(store, sender, strategy, (feed,))
        with pytest.raises(RuntimeError, match="transport crashed"):
            app.process_feed(feed)

    # Then
    with DeliveryStore(database_path) as reopened_store:
        assert reopened_store.has_delivered("news", "entry-1")
        assert not reopened_store.has_delivered("news", "entry-2")


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

        def temporarily_locked(feed_id: str, entry_id: str) -> None:
            nonlocal mark_attempts
            mark_attempts += 1
            if mark_attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            mark_delivered(feed_id, entry_id)

        monkeypatch.setattr(store, "mark_delivered", temporarily_locked)
        monkeypatch.setattr("app.time.sleep", lambda _seconds: None)

        # When
        app.process_feed(feed)

        # Then
        assert store.has_delivered("news", "entry-1")
        assert mark_attempts == 2
        assert len(sender.messages) == 1


def test_shutdown_waits_for_successful_send_to_be_persisted(
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
                raise sqlite3.OperationalError("database is locked")
            mark_delivered(feed_id, entry_id)

        def request_shutdown(_seconds: float) -> None:
            app.request_shutdown()

        monkeypatch.setattr(store, "mark_delivered", temporarily_locked)
        monkeypatch.setattr("app.time.sleep", request_shutdown)

        # When
        app.process_feed(feed)

        # Then
        assert store.has_delivered("news", "entry-1")
        assert mark_attempts == 2
        assert len(sender.messages) == 1
