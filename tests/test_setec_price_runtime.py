from pathlib import Path

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from tests.app_helpers import FakeSender


def test_build_price_jobs_ignores_setec_feeds(tmp_path: Path) -> None:
    # Given
    config = AppConfig(
        feeds=(
            FeedConfig(
                id="setec",
                url="https://setec.example.test/e-prodazba",
                webhook="https://discord.example.test/setec",
                strategy="setec",
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
