from pathlib import Path

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import source_label
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports import HivetecStrategy
from rss2discord.transports.hivetec_price_monitor import HivetecPriceMonitorDependencies
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor


def hivetec_feed(interval: float | None = None) -> FeedConfig:
    return FeedConfig(
        id="hivetec",
        name="Hivetec Products",
        url="https://hivetec.mk/shop/",
        webhook="https://discord.example.test/webhooks/id/token",
        strategy="hivetec",
        price_check_interval=interval,
    )


def test_hivetec_strategy_is_registered_and_labeled(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(), store, FakeSender([]))

    assert isinstance(app._strategies["hivetec"], HivetecStrategy)
    assert source_label(hivetec_feed()) == "Hivetec"


def test_hivetec_price_monitor_builds_an_independent_job(tmp_path: Path) -> None:
    constructed_dependencies: list[HivetecPriceMonitorDependencies] = []
    clock = FakeClock(maximum_sleeps=1)

    def monitor_factory(
        feed: FeedConfig,
        dependencies: HivetecPriceMonitorDependencies,
    ) -> RecordingMonitor:
        constructed_dependencies.append(dependencies)
        return RecordingMonitor(feed.id, [], clock)

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(hivetec_feed(3600),)),
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            hivetec_monitor_factory=monitor_factory,
        )

    assert [job.interval for job in jobs] == [3600]
    assert len(constructed_dependencies) == 1
