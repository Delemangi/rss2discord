import sqlite3

import pytest

from rss2discord.retries import SQLITE_MAX_ATTEMPTS, SQLiteRetryPolicy


def test_sqlite_retry_stops_after_the_attempt_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[float] = []
    monkeypatch.setattr(
        "rss2discord.retries._is_transient_sqlite_error",
        lambda _error: True,
    )

    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    def record_sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        return True

    policy = SQLiteRetryPolicy(
        sleep=record_sleep,
        on_retry=lambda _error, _delay: None,
    )

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        policy.execute(operation)

    assert attempts == SQLITE_MAX_ATTEMPTS
    assert len(sleeps) == SQLITE_MAX_ATTEMPTS - 1


def test_sqlite_retry_still_returns_the_first_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    monkeypatch.setattr(
        "rss2discord.retries._is_transient_sqlite_error",
        lambda _error: True,
    )

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    policy = SQLiteRetryPolicy(
        sleep=lambda _seconds: True,
        on_retry=lambda _error, _delay: None,
    )

    assert policy.execute(operation) == "ok"
    assert attempts == 3
