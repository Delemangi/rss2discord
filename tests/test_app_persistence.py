import sqlite3
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryId
from tests.app_helpers import FakeSender, FakeStrategy, make_app, make_entry, make_feed


def busy_database_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


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
