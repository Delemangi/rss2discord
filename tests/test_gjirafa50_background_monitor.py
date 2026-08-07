from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordWebhookClient
from rss2discord.price_monitor_builders import (
    DEFAULT_PRICE_MONITOR_FACTORIES,
    SharedPriceMonitorDependencies,
    build_provider_price_monitor,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import gjirafa50_background_monitor
from rss2discord.transports.gjirafa50_background_monitor import (
    Gjirafa50BackgroundPriceMonitor,
)
from rss2discord.transports.gjirafa50_price_monitor import (
    Gjirafa50PriceMonitorDependencies,
)
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock
from tests.test_gjirafa50_wiring import make_feed


def test_background_monitor_deduplicates_and_joins_running_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()
    scans = 0

    class BlockingPriceMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: Gjirafa50PriceMonitorDependencies,
        ) -> None:
            del feed, dependencies

        def scan(self) -> None:
            nonlocal scans
            scans += 1
            started.set()
            release.wait(5)

    monkeypatch.setattr(
        gjirafa50_background_monitor,
        "Gjirafa50PriceMonitor",
        BlockingPriceMonitor,
    )
    monitor = _build_background_monitor(tmp_path, "one")

    monitor.scan()
    assert started.wait(2)
    monitor.scan()
    release.set()
    monitor.close()

    assert scans == 1
    assert monitor._thread is not None
    assert not monitor._thread.is_alive()


def test_background_monitor_close_cancels_worker_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = Event()
    sleep_results: list[bool] = []
    shutdown_states: list[bool] = []

    class WaitingPriceMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: Gjirafa50PriceMonitorDependencies,
        ) -> None:
            del feed
            self._dependencies: Gjirafa50PriceMonitorDependencies = dependencies

        def scan(self) -> None:
            started.set()
            sleep_results.extend(
                (
                    self._dependencies.delivery.sleep(30),
                    self._dependencies.fetch_retry_policy.sleep(30),
                    self._dependencies.sqlite_retry_policy.sleep(30),
                ),
            )
            shutdown_states.append(
                self._dependencies.delivery.is_shutdown_requested(),
            )

    monkeypatch.setattr(
        gjirafa50_background_monitor,
        "Gjirafa50PriceMonitor",
        WaitingPriceMonitor,
    )
    monitor = _build_background_monitor(tmp_path, "cancel")

    monitor.scan()
    assert started.wait(2)
    monitor.close()

    assert sleep_results == [False, False, False]
    assert shutdown_states == [True]


def test_background_monitor_close_cancels_worker_waiting_for_scan_lock(
    tmp_path: Path,
) -> None:
    closed = Event()
    monitor = _build_background_monitor(tmp_path, "queued")

    def close_monitor() -> None:
        monitor.close()
        closed.set()

    assert Gjirafa50BackgroundPriceMonitor._scan_lock.acquire(timeout=1)
    monitor.scan()
    close_thread = Thread(target=close_monitor)
    close_thread.start()

    try:
        assert closed.wait(1)
    finally:
        Gjirafa50BackgroundPriceMonitor._scan_lock.release()
        close_thread.join(2)

    assert not close_thread.is_alive()


def test_background_monitors_serialize_cross_feed_catalog_scans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_lock = Lock()
    started = {"one": Event(), "two": Event()}
    release = {"one": Event(), "two": Event()}
    active = 0
    maximum_active = 0

    class BlockingPriceMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: Gjirafa50PriceMonitorDependencies,
        ) -> None:
            del dependencies
            self._feed_id = feed.id

        def scan(self) -> None:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            started[self._feed_id].set()
            release[self._feed_id].wait(5)
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        gjirafa50_background_monitor,
        "Gjirafa50PriceMonitor",
        BlockingPriceMonitor,
    )
    first = _build_background_monitor(tmp_path, "one")
    second = _build_background_monitor(tmp_path, "two")

    first.scan()
    assert started["one"].wait(2)
    second.scan()
    second_started_while_first_active = started["two"].wait(0.1)
    release["one"].set()
    assert started["two"].wait(2)
    release["two"].set()
    first.close()
    second.close()

    assert not second_started_while_first_active
    assert maximum_active == 1


def test_background_worker_constructs_thread_local_store_and_sender(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[Gjirafa50PriceMonitorDependencies] = []

    class RecordingPriceMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: Gjirafa50PriceMonitorDependencies,
        ) -> None:
            del feed
            captured.append(dependencies)

        def scan(self) -> None:
            return

    monkeypatch.setattr(
        gjirafa50_background_monitor,
        "Gjirafa50PriceMonitor",
        RecordingPriceMonitor,
    )
    monitor = _build_background_monitor(tmp_path, "isolated")

    monitor.scan()
    monitor.close()

    assert len(captured) == 1
    assert isinstance(captured[0].snapshots, DeliveryStore)
    assert isinstance(captured[0].sender, DiscordWebhookClient)


def _build_background_monitor(
    tmp_path: Path,
    feed_id: str,
) -> Gjirafa50BackgroundPriceMonitor:
    clock = FakeClock(maximum_sleeps=1)
    with DeliveryStore(tmp_path / f"{feed_id}.db") as store:
        monitor = build_provider_price_monitor(
            make_feed(interval=21_600).model_copy(update={"id": feed_id}),
            SharedPriceMonitorDependencies(
                snapshots=store,
                sender=FakeSender([]),
                fetch_retry_policy=FetchRetryPolicy(
                    sleep=clock.sleep,
                    on_retry=lambda error, delay: None,
                ),
                sqlite_retry_policy=SQLiteRetryPolicy(
                    sleep=clock.sleep,
                    on_retry=lambda error, delay: None,
                ),
                delivery=PriceAlertDelivery(clock.sleep, 0, lambda: False),
                pazar3_pacer=Pazar3RequestPacer(clock.monotonic),
            ),
            DEFAULT_PRICE_MONITOR_FACTORIES,
        )
    assert isinstance(monitor, Gjirafa50BackgroundPriceMonitor)
    return monitor
