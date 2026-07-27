from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.transports import FeedFetchError, NeksioStrategy
from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from rss2discord.transports.neksio_models import NeksioProduct
from tests.app_helpers import FakeSender


def test_app_registers_neksio_strategy(tmp_path: Path) -> None:
    # Given / When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config=AppConfig(), store=store, sender=FakeSender([]))

    # Then
    assert isinstance(app._strategies["neksio"], NeksioStrategy)


def test_neksio_first_fetch_seeds_current_products_before_later_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_product = NeksioProduct(
        product_id=1,
        product_name="First product",
        product_code="FIRST",
        category="Components",
        subcategory="Graphics",
        manufacturer="Example",
        price_with_tax=Decimal(1200),
        formatted_price="1.200 ден.",
        image_path="/images/1.png",
        stock_quantity=7,
        observed_at=datetime.now(UTC),
    )
    later_product = first_product.model_copy(
        update={
            "product_id": 2,
            "product_name": "Later product",
            "product_code": "LATER",
            "image_path": "/images/2.png",
        },
    )
    products = [first_product]
    shutdown_callbacks: list[Callable[[], bool]] = []

    def fetch_catalog(
        client: NeksioCatalogClient,
        url: str,
        *,
        is_shutdown_requested: Callable[[], bool] | None = None,
    ) -> tuple[NeksioProduct, ...]:
        del client
        assert url == "https://g.store.neksio.mk/"
        assert is_shutdown_requested is not None
        shutdown_callbacks.append(is_shutdown_requested)
        return tuple(products)

    monkeypatch.setattr(NeksioCatalogClient, "fetch_catalog", fetch_catalog)
    feed = FeedConfig(
        id="neksio-products",
        url="https://g.store.neksio.mk/",
        webhook="https://discord.test/neksio",
        strategy="neksio",
    )
    sender = FakeSender([True])
    config = AppConfig(delay_between_posts=0, max_post_age_days=0, feeds=(feed,))

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config=config, store=store, sender=sender)
        app.process_feed(feed)
        messages_after_seed = list(sender.messages)
        products.append(later_product)
        app.process_feed(feed)

        # Then
        assert store.is_feed_initialized(feed.id)
        assert store.has_delivered(feed.id, "1")
        assert store.has_delivered(feed.id, "2")

    assert messages_after_seed == []
    assert [message.entry.title for message in sender.messages] == ["Later product"]
    assert sender.messages[0].source_title == "Neksio"
    assert shutdown_callbacks == [app.is_shutdown_requested, app.is_shutdown_requested]


def test_neksio_empty_first_fetch_does_not_initialize_the_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        NeksioCatalogClient,
        "fetch_catalog",
        lambda client, url, *, is_shutdown_requested=None: (),
    )
    feed = FeedConfig(
        id="neksio-products",
        url="https://g.store.neksio.mk/",
        webhook="https://discord.test/neksio",
        strategy="neksio",
    )

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, FakeSender([]))
        app.process_feed(feed)

        # Then
        assert not store.is_feed_initialized(feed.id)


def test_neksio_rejects_too_many_new_products_before_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first = NeksioProduct(
        product_id=1,
        product_name="First",
        product_code="FIRST",
        category="Components",
        subcategory="Graphics",
        manufacturer="Example",
        price_with_tax=Decimal(1200),
        formatted_price="1.200 MKD",
        image_path="/images/1.png",
        stock_quantity=1,
        observed_at=datetime.now(UTC),
    )
    products = (
        first,
        first.model_copy(update={"product_id": 2, "product_name": "Second"}),
    )
    monkeypatch.setattr(NeksioStrategy, "max_new_entries_per_fetch", 1, raising=False)
    monkeypatch.setattr(
        NeksioCatalogClient,
        "fetch_catalog",
        lambda client, url, *, is_shutdown_requested=None: products,
    )
    feed = FeedConfig(
        id="neksio-products",
        url="https://g.store.neksio.mk/",
        webhook="https://discord.test/neksio",
        strategy="neksio",
    )

    # When / Then
    with DeliveryStore(tmp_path / "state.db") as store:
        store.seed_feed(feed.id, ())
        sender = FakeSender([True, True])
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, sender)
        with pytest.raises(FeedFetchError, match="NewEntryLimitExceeded"):
            app.process_feed(feed)
        assert sender.messages == []
        assert not store.has_delivered(feed.id, "1")


def test_neksio_rejects_delivery_history_growth_past_the_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = NeksioProduct(
        product_id=3,
        product_name="Third",
        product_code="THIRD",
        category="Components",
        subcategory="Graphics",
        manufacturer="Example",
        price_with_tax=Decimal(1200),
        formatted_price="1.200 MKD",
        image_path="/images/3.png",
        stock_quantity=1,
        observed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(NeksioStrategy, "max_delivery_history", 2, raising=False)
    monkeypatch.setattr(
        NeksioCatalogClient,
        "fetch_catalog",
        lambda client, url, *, is_shutdown_requested=None: (product,),
    )
    feed = FeedConfig(
        id="neksio-products",
        url="https://g.store.neksio.mk/",
        webhook="https://discord.test/neksio",
        strategy="neksio",
    )

    # When / Then
    with DeliveryStore(tmp_path / "state.db") as store:
        store.seed_feed(feed.id, ("1", "2"))
        sender = FakeSender([True])
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, sender)
        with pytest.raises(FeedFetchError, match="DeliveryHistoryLimitExceeded"):
            app.process_feed(feed)
        assert sender.messages == []
        assert not store.has_delivered(feed.id, "3")
