from pathlib import Path

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_monitor_builders import (
    DEFAULT_PRICE_MONITOR_FACTORIES,
    SharedPriceMonitorDependencies,
    build_provider_price_monitor,
)
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
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
