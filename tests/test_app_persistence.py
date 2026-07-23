import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord import delivery_store
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryId
from rss2discord.retries import SQLiteRetryPolicy
from tests.app_helpers import FakeSender, FakeStrategy, make_app, make_entry, make_feed


def busy_database_error() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def test_delivery_persistence_characterizes_existing_immediate_marking(
    tmp_path: Path,
) -> None:
    # Given
    feed = make_feed("news")

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, FakeSender([]), FakeStrategy([]), (feed,))

        # When
        persisted = app._persist_delivery(feed.id, EntryId("entry-1"))

        # Then
        assert persisted
        assert store.has_delivered(feed.id, "entry-1")


def test_snapshot_operation_retries_with_the_shared_transient_sqlite_policy(
    tmp_path: Path,
) -> None:
    # Given
    snapshot = delivery_store.PriceSnapshot(
        feed_id="anhoch",
        product_id=1,
        amount=Decimal("99.95"),
        formatted="99.95 den",
        currency="MKD",
    )
    retry_delays: list[float] = []
    write_attempts = 0

    with DeliveryStore(tmp_path / "state.db") as store:

        def record_retry_sleep(seconds: float) -> bool:
            retry_delays.append(seconds)
            return True

        def upsert_snapshot() -> None:
            nonlocal write_attempts
            write_attempts += 1
            if write_attempts == 1:
                raise busy_database_error()
            store.upsert_price_snapshot(snapshot)

        retry_policy = SQLiteRetryPolicy(
            sleep=record_retry_sleep,
            on_retry=lambda error, delay: None,
        )

        # When
        retry_policy.execute(upsert_snapshot)

        # Then
        assert store.load_price_snapshots("anhoch") == (snapshot,)

    assert write_attempts == 2
    assert retry_delays == [5.0]


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


def test_app_delivery_persistence_survives_price_snapshot_schema_initialization(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "state.db"
    feed = make_feed("news")
    sender = FakeSender([True])
    strategy = FakeStrategy([make_entry("entry-1")])
    snapshot = delivery_store.PriceSnapshot(
        feed_id="anhoch",
        product_id=1,
        amount=Decimal("99.95"),
        formatted="99.95 ден",
        currency="MKD",
    )

    # When
    with DeliveryStore(database_path) as store:
        app = make_app(store, sender, strategy, (feed,))
        app.process_feed(feed)
        store.upsert_price_snapshot(snapshot)

    with DeliveryStore(database_path) as reopened_store:
        delivered = reopened_store.has_delivered("news", "entry-1")
        snapshots = reopened_store.load_price_snapshots("anhoch")

    # Then
    assert delivered
    assert snapshots == (snapshot,)
