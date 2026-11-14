import sqlite3
from pathlib import Path

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.gjirafa50 import (
    background_monitor as gjirafa50_background_monitor,
)
from rss2discord.retries import FeedFetchInterruptedError, SQLiteRetryInterruptedError
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
def test_fetch_failure_surfaces_without_pausing_or_hiding_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        sender = RecordingSender([])
        monitor: Monitor = (
            monitor_for(provider, [(100,) * 101, (90,) * 101], store, sender)
            if provider in {"ddstore", "hivetec"}
            else build(provider, 101, store, sender)
        )
        monitor.scan()
        before = store.load_price_snapshots(provider)

        def fail() -> None:
            raise FeedFetchError(provider, "IncompleteCatalog", status_code=200)

        monkeypatch.setattr(monitor, "_scan", fail)
        with pytest.raises(FeedFetchError) as raised:
            monitor.scan()
        assert raised.value.cause_type == "IncompleteCatalog"
        assert not sender.messages
        assert store.load_price_snapshots(provider) == before


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
