from pathlib import Path

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports.setec_catalog import SetecCatalogClient
from rss2discord.transports.setec_price_monitor import SetecPriceMonitorDependencies
from tests.app_helpers import FakeSender
from tests.runtime_helpers import FakeClock, RecordingMonitor


def test_build_price_jobs_ignores_disabled_setec_feeds(tmp_path: Path) -> None:
    # Given
    config = AppConfig(
        feeds=(
            FeedConfig(
                id="setec",
                url="https://setec.example.test/e-prodazba",
                webhook="https://discord.example.test/setec",
                strategy="setec",
                price_check_interval=None,
            ),
        ),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        dependencies = PriceJobDependencies(
            store=store,
            sender=FakeSender([]),
            sleep=lambda _seconds: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        )

        # When
        jobs = build_price_jobs(config, dependencies)

    # Then
    assert jobs == ()


def test_build_price_jobs_constructs_enabled_setec_job_with_its_interval(
    tmp_path: Path,
) -> None:
    # Given
    clock = FakeClock(maximum_sleeps=1)
    constructed_feed_ids: list[str] = []
    constructed_dependencies: list[SetecPriceMonitorDependencies] = []
    config = AppConfig(
        feeds=(
            FeedConfig(
                id="setec",
                url="https://setec.example.test/e-prodazba",
                webhook="https://discord.example.test/setec",
                strategy="setec",
                price_check_interval=11,
            ),
        ),
    )

    def setec_monitor_factory(
        feed: FeedConfig,
        dependencies: SetecPriceMonitorDependencies,
    ) -> RecordingMonitor:
        constructed_feed_ids.append(feed.id)
        constructed_dependencies.append(dependencies)
        return RecordingMonitor(feed.id, [], clock)

    with DeliveryStore(tmp_path / "state.db") as store:
        dependencies = PriceJobDependencies(
            store=store,
            sender=FakeSender([]),
            sleep=clock.sleep,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        )

        # When
        jobs = build_price_jobs(
            config,
            dependencies,
            setec_monitor_factory=setec_monitor_factory,
        )

    # Then
    assert constructed_feed_ids == ["setec"]
    assert [job.interval for job in jobs] == [11]
    assert isinstance(constructed_dependencies[0].catalog, SetecCatalogClient)
