from pathlib import Path

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports.reklama5_catalog import Reklama5CatalogClient
from rss2discord.transports.reklama5_price_monitor import (
    Reklama5PriceMonitorDependencies,
)
from tests.app_helpers import FakeSender
from tests.reklama5_helpers import SEARCH_URL
from tests.runtime_helpers import FakeClock, RecordingMonitor


def test_build_price_jobs_wires_enabled_reklama5_category_monitor(
    tmp_path: Path,
) -> None:
    clock = FakeClock(maximum_sleeps=1)
    constructed: list[Reklama5PriceMonitorDependencies] = []
    feed = FeedConfig(
        id="reklama5",
        url=SEARCH_URL,
        webhook="https://discord.test/webhook",
        strategy="reklama5",
        price_check_interval=17,
    )

    def monitor_factory(
        built_feed: FeedConfig,
        dependencies: Reklama5PriceMonitorDependencies,
    ) -> RecordingMonitor:
        assert built_feed is feed
        constructed.append(dependencies)
        return RecordingMonitor(built_feed.id, [], clock)

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(feed,)),
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=clock.sleep,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            reklama5_monitor_factory=monitor_factory,
        )

    assert [job.interval for job in jobs] == [17]
    assert len(constructed) == 1
    assert isinstance(constructed[0].catalog, Reklama5CatalogClient)
