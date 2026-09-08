from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig, load_config
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import source_label
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports import TechnomarketStrategy
from rss2discord.transports.technomarket_catalog import TechnomarketCatalogClient
from rss2discord.transports.technomarket_models import TechnomarketProduct
from tests.app_helpers import FakeSender


def test_app_registers_technomarket_strategy(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(), store, FakeSender([]))

    assert isinstance(app._strategies["technomarket"], TechnomarketStrategy)


def test_config_example_is_parseable() -> None:
    config = load_config(Path(__file__).parents[1] / "config" / "config.example.yaml")

    assert any(feed.strategy == "technomarket" for feed in config.feeds)


def test_build_price_jobs_accepts_technomarket_feed(tmp_path: Path) -> None:
    config = AppConfig(
        feeds=(
            FeedConfig(
                id="technomarket",
                url="https://tehnomarket.com.mk/category/4003/laptopi",
                webhook="https://discord.example.test/webhook",
                strategy="technomarket",
                price_check_interval=3600,
            ),
        ),
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            config,
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=lambda _: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        )

    assert [job.interval for job in jobs] == [3600]


def test_technomarket_source_label_is_stable() -> None:
    feed = FeedConfig(
        id="technomarket",
        url="https://tehnomarket.com.mk/category/4003/laptopi",
        webhook="https://discord.example.test/webhook",
        strategy="technomarket",
    )

    assert source_label(feed) == "Technomarket"


def test_technomarket_discovery_timestamp_allows_new_product_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = FeedConfig(
        id="technomarket",
        url="https://tehnomarket.com.mk/category/4003/laptopi",
        webhook="https://discord.example.test/webhook",
        strategy="technomarket",
    )
    observed_at = datetime.now(UTC)
    products = [
        TechnomarketProduct(
            product_id="29404051",
            name="Existing laptop",
            url="https://tehnomarket.com.mk/product/29404051/existing",
            image_url=None,
            manufacturer="ACER",
            categories=(),
            regular_price=Decimal(20999),
            smart_price=Decimal(19499),
            observed_at=observed_at,
        ),
        TechnomarketProduct(
            product_id="29400351",
            name="New laptop",
            url="https://tehnomarket.com.mk/product/29400351/new",
            image_url=None,
            manufacturer="DELL",
            categories=(),
            regular_price=Decimal(22999),
            smart_price=None,
            observed_at=observed_at,
        ),
    ]
    batches = iter(((products[0],), (products[0], products[1])))
    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "fetch_latest_products",
        lambda self, url, is_shutdown_requested=lambda: False: next(batches),
    )
    sender = FakeSender([True])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, sender)
        app.process_feed(feed)
        app.process_feed(feed)

    assert [message.entry.title for message in sender.messages] == ["New laptop"]
