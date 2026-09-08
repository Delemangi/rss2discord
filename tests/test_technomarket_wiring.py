from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig, load_config
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.source_labels import source_label
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.transports import TechnomarketStrategy
from rss2discord.transports.technomarket_catalog import (
    TECHNOMARKET_FEED_URL,
    TechnomarketCatalogClient,
)
from tests.app_helpers import FakeSender

FIXTURES = Path(__file__).parent / "fixtures" / "technomarket"


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
    first_page = (FIXTURES / "category-page-1.html").read_text(encoding="utf-8")
    second_page = (FIXTURES / "category-page-2.html").read_text(encoding="utf-8")
    changed_second_page = second_page.replace("29400003", "29400004")
    responses = iter((first_page, second_page, first_page, changed_second_page))
    calls: list[str] = []

    def fetch(url: str, **kwargs: object) -> str:
        del kwargs
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(
        TechnomarketCatalogClient,
        "_fetch_html",
        staticmethod(fetch),
    )
    sender = FakeSender([True])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, sender)
        app.process_feed(feed)
        app.process_feed(feed)

    assert [message.entry.title for message in sender.messages] == ["Notebook Gamma"]
    assert calls == [
        TECHNOMARKET_FEED_URL,
        f"{TECHNOMARKET_FEED_URL}?page=2",
        TECHNOMARKET_FEED_URL,
        f"{TECHNOMARKET_FEED_URL}?page=2",
    ]
