from pathlib import Path

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports.neptun_catalog import NeptunCatalogClient
from rss2discord.transports.neptun_price_monitor import NeptunPriceMonitorDependencies
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor
from tests.test_neptun_wiring import make_feed


def test_runtime_builds_enabled_neptun_monitor_with_category_catalog(
    tmp_path: Path,
) -> None:
    clock = FakeClock(maximum_sleeps=1)
    constructed: list[NeptunPriceMonitorDependencies] = []

    def monitor_factory(
        feed: FeedConfig,
        dependencies: NeptunPriceMonitorDependencies,
    ) -> RecordingMonitor:
        del feed
        constructed.append(dependencies)
        return RecordingMonitor("neptun", [], clock)

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(make_feed(interval=11),)),
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            neptun_monitor_factory=monitor_factory,
        )

    assert [job.interval for job in jobs] == [11]
    assert isinstance(constructed[0].catalog, NeptunCatalogClient)
