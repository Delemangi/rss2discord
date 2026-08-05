from pathlib import Path
from threading import Event, Lock

import pytest

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordWebhookClient
from rss2discord.price_monitor_builders import (
    DEFAULT_PRICE_MONITOR_FACTORIES,
    SharedPriceMonitorDependencies,
    build_provider_price_monitor,
)
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports import gjirafa50_background_monitor
from rss2discord.transports.gjirafa50_background_monitor import (
    Gjirafa50BackgroundPriceMonitor,
)
from rss2discord.transports.gjirafa50_catalog import Gjirafa50CatalogClient
from rss2discord.transports.gjirafa50_price_monitor import (
    Gjirafa50PriceMonitorDependencies,
)
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor
from tests.test_gjirafa50_wiring import make_feed


def test_runtime_builds_enabled_gjirafa50_monitor(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=1)
    constructed: list[Gjirafa50PriceMonitorDependencies] = []

    def monitor_factory(
        feed: FeedConfig,
        dependencies: Gjirafa50PriceMonitorDependencies,
    ) -> RecordingMonitor:
        del feed
        constructed.append(dependencies)
        return RecordingMonitor("gjirafa50", [], clock)

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(make_feed(interval=21_600),)),
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            gjirafa50_monitor_factory=monitor_factory,
        )

    # Then
    assert [job.interval for job in jobs] == [21_600]
    assert isinstance(constructed[0].catalog, Gjirafa50CatalogClient)


def test_runtime_wires_gjirafa50_monitor_shutdown(tmp_path: Path) -> None:
    clock = FakeClock(maximum_sleeps=1)
    events: list[str] = []

    class ClosableMonitor:
        def scan(self) -> None:
            events.append("scan")

        def close(self) -> None:
            events.append("close")

    def monitor_factory(
        feed: FeedConfig,
        dependencies: Gjirafa50PriceMonitorDependencies,
    ) -> ClosableMonitor:
        del feed, dependencies
        return ClosableMonitor()

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(make_feed(interval=21_600),)),
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            gjirafa50_monitor_factory=monitor_factory,
        )

    jobs[0].run()
    assert jobs[0].close is not None
    jobs[0].close()

    assert events == ["scan", "close"]


def test_default_factory_uses_background_gjirafa50_monitor(tmp_path: Path) -> None:
    clock = FakeClock(maximum_sleeps=1)

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = build_provider_price_monitor(
            make_feed(interval=21_600),
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
