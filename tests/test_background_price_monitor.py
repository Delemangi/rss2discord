from pathlib import Path
from threading import Event

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordWebhookClient
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.pazar3.pacing import Pazar3RequestPacer
from rss2discord.providers.registry import (
    BackgroundPriceMonitor,
    SharedPriceMonitorDependencies,
)
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import PriceAlertDelivery
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="reklama5-price",
        url="https://reklama5.mk/search",
        webhook="https://discord.test/webhooks/id/token",
        strategy="reklama5",
        price_check_interval=3600,
    )


def make_dependencies(
    store: DeliveryStore,
    clock: FakeClock,
) -> SharedPriceMonitorDependencies:
    return SharedPriceMonitorDependencies(
        snapshots=store,
        database_path=store.database_path,
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
    )


def test_background_price_monitor_deduplicates_and_joins(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()
    scans = 0

    class BlockingMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: SharedPriceMonitorDependencies,
        ) -> None:
            del feed, dependencies

        def scan(self) -> None:
            nonlocal scans
            scans += 1
            started.set()
            release.wait(5)

    clock = FakeClock(maximum_sleeps=1)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = BackgroundPriceMonitor(
            make_feed(),
            make_dependencies(store, clock),
            BlockingMonitor,
        )

        monitor.scan()
        assert started.wait(2)
        monitor.scan()
        release.set()
        monitor.close()

    assert scans == 1
    assert monitor._thread is not None
    assert not monitor._thread.is_alive()


def test_background_price_monitor_uses_thread_local_resources(
    tmp_path: Path,
) -> None:
    captured: list[SharedPriceMonitorDependencies] = []

    class CapturingMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: SharedPriceMonitorDependencies,
        ) -> None:
            del feed
            captured.append(dependencies)

        def scan(self) -> None:
            return

    clock = FakeClock(maximum_sleeps=1)
    with DeliveryStore(tmp_path / "state.db") as store:
        main_dependencies = make_dependencies(store, clock)
        monitor = BackgroundPriceMonitor(
            make_feed(),
            main_dependencies,
            CapturingMonitor,
        )

        monitor.scan()
        monitor.close()

    assert len(captured) == 1
    worker_dependencies = captured[0]
    assert isinstance(worker_dependencies.snapshots, DeliveryStore)
    assert isinstance(worker_dependencies.sender, DiscordWebhookClient)
    assert worker_dependencies.snapshots is not main_dependencies.snapshots
    assert worker_dependencies.sender is not main_dependencies.sender


def test_background_price_monitor_records_failure(
    tmp_path: Path,
) -> None:
    class FailingMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: SharedPriceMonitorDependencies,
        ) -> None:
            del feed, dependencies

        def scan(self) -> None:
            raise FeedFetchError("Reklama5", "CatalogChanged")

    clock = FakeClock(maximum_sleeps=1)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = BackgroundPriceMonitor(
            make_feed(),
            make_dependencies(store, clock),
            FailingMonitor,
        )

        monitor.scan()
        monitor.close()

    with DeliveryStore(tmp_path / "state.db") as store:
        records = store.list_health("reklama5-price")

    price_records = [record for record in records if record.job_kind == "price"]
    assert len(price_records) == 1
    assert price_records[0].state == "failed"
    assert price_records[0].cause == "CatalogChanged"


def test_background_price_monitor_close_cancels_worker_wait(
    tmp_path: Path,
) -> None:
    started = Event()
    sleep_results: list[bool] = []
    shutdown_states: list[bool] = []

    class WaitingMonitor:
        def __init__(
            self,
            feed: FeedConfig,
            dependencies: SharedPriceMonitorDependencies,
        ) -> None:
            del feed
            self._dependencies = dependencies

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

    clock = FakeClock(maximum_sleeps=1)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = BackgroundPriceMonitor(
            make_feed(),
            make_dependencies(store, clock),
            WaitingMonitor,
        )

        monitor.scan()
        assert started.wait(2)
        monitor.close()

    assert sleep_results == [False, False, False]
    assert shutdown_states == [True]
