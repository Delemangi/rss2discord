import sqlite3
import time
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.retries import FeedFetchInterruptedError, SQLiteRetryInterruptedError
from rss2discord.transports import gjirafa50_background_monitor
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_adapter_c2_provider_caps import build
from tests.test_gjirafa50_background_monitor import _build_background_monitor


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
    ],
)
@pytest.mark.parametrize(
    ("cause", "status"),
    [("AccessChallenge", 503), ("BotChallenge", 200), ("HTTPError", 403)],
)
def test_consumed_challenge_pauses_batch_and_preserves_blocked_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    cause: str,
    status: int,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        sender = RecordingSender([])
        monitor = build(provider, 101, store, sender)
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
        before = int(time.time())
        monitor.scan()
        batch = store.load_active_price_batch(provider)
        assert batch is not None
        assert batch.status == "paused"
        health = store.list_health(provider)[0]
        assert health.state == "blocked"
        assert health.cause == cause
        assert (store.get_blocked_until(provider) or 0) >= before + 21_600
        assert not sender.messages
        assert all(
            snapshot.amount == 100 for snapshot in store.load_price_snapshots(provider)
        )


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
