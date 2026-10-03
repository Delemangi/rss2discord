import logging
import sqlite3
import time
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_runtime import feed_is_blocked
from rss2discord.retries import FeedFetchInterruptedError, SQLiteRetryInterruptedError
from rss2discord.transports import gjirafa50_background_monitor
from rss2discord.transports.price_monitor import pause_price_fetch_failure
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_adapter_c2_provider_caps import Monitor, build
from tests.test_gjirafa50_background_monitor import _build_background_monitor
from tests.test_price_recovery_integration import monitor_for


@pytest.mark.parametrize(
    "provider",
    [
        "anhoch",
        "neksio",
        "setec",
        "cccenter",
        "gjirafa50",
        "neptun",
        "pazar3",
        "reklama5",
        "technomarket",
        "ddstore",
        "hivetec",
    ],
)
@pytest.mark.parametrize(
    ("cause", "status", "state"),
    [
        ("AccessChallenge", 503, "blocked"),
        ("BotChallenge", 200, "blocked"),
        ("HTTPError", 403, "blocked"),
        ("IncompleteCatalog", 200, "recovery_required"),
    ],
)
def test_consumed_failure_records_one_outcome_and_preserves_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    provider: str,
    cause: str,
    status: int,
    state: str,
) -> None:
    now = int(time.time())
    monkeypatch.setattr("rss2discord.transports.price_monitor.time.time", lambda: now)
    caplog.set_level(logging.INFO, logger="rss2discord.transports.price_monitor")
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        sender = RecordingSender([])
        monitor: Monitor = (
            monitor_for(provider, [(100,) * 101, (90,) * 101], store, sender)
            if provider in {"ddstore", "hivetec"}
            else build(provider, 101, store, sender)
        )
        monitor.scan()
        monitor.scan()
        candidate = store.list_price_change_batches(feed_id=provider)[0]
        store.approve_price_change_batch(
            feed_id=provider,
            fingerprint=candidate.fingerprint,
            reason="fixture review",
        )

        def fail() -> None:
            raise FeedFetchError(provider, cause, status_code=status)

        monkeypatch.setattr(monitor, "_scan", fail)
        before = store.list_health(provider)[0]
        snapshots = store.load_price_snapshots(provider)
        caplog.clear()
        monitor.scan()
        batch = store.load_active_price_batch(provider)
        assert batch is not None
        assert batch.status == "paused"
        health = store.list_health(provider)[0]
        assert health.state == state
        assert health.cause == cause
        assert health.total_attempts == before.total_attempts + 1
        assert health.consecutive_failures == before.consecutive_failures + 1
        assert health.total_successes == before.total_successes
        assert health.last_success_at == before.last_success_at
        assert health.transition_at == now
        assert caplog.messages == [
            f"Price health for feed {provider}: {state} ({cause})",
        ]
        deadline = now + 21_600 if state == "blocked" else None
        assert store.get_blocked_until(provider) == deadline
        assert not sender.messages
        assert store.load_price_snapshots(provider) == snapshots

    with DeliveryStore(path) as reopened:
        assert reopened.list_health(provider)[0] == health
        assert reopened.get_blocked_until(provider) == deadline
        assert reopened.load_price_snapshots(provider) == snapshots
        if deadline is not None:
            feed = FeedConfig(
                id=provider,
                url="https://example.test/catalog",
                webhook="https://discord.example.test/hook",
            )
            assert feed_is_blocked(feed, (feed,), reopened, deadline - 1)
            assert not feed_is_blocked(feed, (feed,), reopened, deadline)
            assert reopened.list_health(provider)[0] == health
            monkeypatch.setattr(
                "rss2discord.transports.price_monitor.time.time",
                lambda: deadline + 1,
            )
            caplog.clear()
            error = FeedFetchError(provider, cause, status_code=status)
            assert pause_price_fetch_failure(reopened, provider, error)
            retried = reopened.list_health(provider)[0]
            assert retried.total_attempts == health.total_attempts + 1
            assert retried.consecutive_failures == health.consecutive_failures + 1
            assert retried.total_successes == health.total_successes
            assert reopened.get_blocked_until(provider) == deadline + 21_601
            assert caplog.messages == [
                f"Price health for feed {provider}: blocked ({cause})",
            ]


@pytest.mark.parametrize("cause", ["BotChallenge", "IncompleteCatalog"])
def test_failure_without_active_recovery_leaves_health_to_caller(
    tmp_path: Path,
    cause: str,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        error = FeedFetchError("fixture", cause)
        assert not pause_price_fetch_failure(store, "fixture", error)
        assert store.list_health("fixture") == ()
        assert store.list_price_change_batches("fixture") == ()


@pytest.mark.parametrize(
    ("error", "state", "cause"),
    [
        (
            FeedFetchError("Gjirafa50", "ProductWorkLimitExceeded"),
            "failed",
            "ProductWorkLimitExceeded",
        ),
        (
            FeedFetchError("Gjirafa50", "AccessChallenge", status_code=503),
            "blocked",
            "AccessChallenge",
        ),
        (FeedFetchError("Gjirafa50", "BotChallenge"), "blocked", "BotChallenge"),
        (
            FeedFetchError("Gjirafa50", "HTTPError", status_code=403),
            "blocked",
            "HTTPError",
        ),
        (sqlite3.OperationalError("hidden-secret"), "failed", "OperationalError"),
        (RuntimeError("hidden-secret"), "failed", "RuntimeError"),
        (FeedFetchError("Gjirafa50", "https://hidden-secret"), "failed", "FetchError"),
    ],
)
def test_worker_persists_failure_health_using_thread_local_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    error: Exception,
    state: str,
    cause: str,
) -> None:
    calls = 0

    def fail_scan(self: object) -> None:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(
        gjirafa50_background_monitor.Gjirafa50PriceMonitor,
        "scan",
        fail_scan,
    )
    monitor = _build_background_monitor(tmp_path, "worker")
    try:
        monitor.scan()
        assert monitor._thread is not None
        monitor._thread.join(3)
        assert not monitor._thread.is_alive()
        with DeliveryStore(tmp_path / "worker.db") as store:
            health = store.list_health("worker")[0]
            assert health.state == state
            assert health.cause == cause
            assert health.duration_ms is not None
            assert health.duration_ms >= 0
            assert bool(store.get_blocked_until("worker")) == (state == "blocked")
        if state == "blocked":
            monitor.scan()
            assert monitor._thread is not None
            monitor._thread.join(3)
            assert not monitor._thread.is_alive()
            assert calls == 1
    finally:
        monitor.close()
    assert "hidden-secret" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [FeedFetchInterruptedError(), SQLiteRetryInterruptedError()],
)
def test_worker_interruption_does_not_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def interrupt(self: object) -> None:
        raise error

    monkeypatch.setattr(
        gjirafa50_background_monitor.Gjirafa50PriceMonitor,
        "scan",
        interrupt,
    )
    monitor = _build_background_monitor(tmp_path, "interrupted")
    try:
        monitor.scan()
        assert monitor._thread is not None
        monitor._thread.join(3)
        assert not monitor._thread.is_alive()
    finally:
        monitor.close()
    with DeliveryStore(tmp_path / "interrupted.db") as store:
        assert store.list_health("interrupted") == ()
