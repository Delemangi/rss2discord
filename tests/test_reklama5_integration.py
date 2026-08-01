from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig, load_config
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryId
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import reklama5_http
from rss2discord.transports.reklama5 import Reklama5Listing, Reklama5Strategy
from tests.app_helpers import FakeSender
from tests.configuration_helpers import write_config
from tests.reklama5_helpers import (
    FIXED_NOW,
    SEARCH_URL,
    RecordingGet,
    Reklama5Card,
    StubResponse,
    search_page,
)


class SequencedReklama5Strategy(Reklama5Strategy):
    def __init__(self, batches: list[list[Reklama5Listing]]) -> None:
        super().__init__(clock=lambda: FIXED_NOW)
        self._batches = batches

    def fetch_entries(self, url: str) -> tuple[list[Reklama5Listing], str]:
        assert url == SEARCH_URL
        return self._batches.pop(0), "Reklama5"


def _listing() -> Reklama5Listing:
    return Reklama5Listing(
        entry_id=EntryId("123"),
        url="https://reklama5.mk/AdDetails?ad=123",
        title="Listing",
        summary="Summary",
        price="100 ден.",
        location="Скопје",
        category="Компјутери",
        activity_at=FIXED_NOW,
        image_url=None,
    )


def _feed() -> FeedConfig:
    return FeedConfig(
        id="reklama5-computer-parts",
        name="Reklama5 Computer Parts",
        url=SEARCH_URL,
        webhook="https://discord.test/webhook",
        strategy="reklama5",
    )


def _app(
    store: DeliveryStore,
    sender: FakeSender,
    strategy: Reklama5Strategy,
) -> RSSToDiscord:
    feed = _feed()
    app = RSSToDiscord(
        AppConfig(delay_between_posts=0, max_post_age_days=0, feeds=(feed,)),
        store,
        sender,
    )
    app._strategies["reklama5"] = strategy
    return app


def test_load_config_parses_reklama5_strategy(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: reklama5\n"
        f"    url: {SEARCH_URL}\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: reklama5\n",
    )

    config = load_config(config_path)

    assert config.feeds[0].strategy == "reklama5"


def test_load_config_rejects_adapter_with_reklama5_strategy(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: reklama5\n"
        f"    url: {SEARCH_URL}\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: reklama5\n"
        "    adapter: hackernews\n",
    )

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_price_check_interval_with_reklama5_strategy(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: reklama5\n"
        f"    url: {SEARCH_URL}\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: reklama5\n"
        "    price_check_interval: 3600\n",
    )

    with pytest.raises(ValidationError) as validation_error:
        load_config(config_path)

    assert (
        "price_check_interval requires the anhoch, ddstore, neksio, or setec strategy"
        in str(validation_error.value)
    )


def test_app_registers_reklama5_strategy(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(), store, FakeSender([]))

    assert isinstance(app._strategies["reklama5"], Reklama5Strategy)


def test_reklama5_non_empty_first_fetch_seeds_without_delivery(tmp_path: Path) -> None:
    feed = _feed()
    sender = FakeSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = _app(store, sender, SequencedReklama5Strategy([[_listing()]]))

        app.process_feed(feed)

        assert store.is_feed_initialized(feed.id)
        assert store.has_delivered(feed.id, "123")
    assert sender.messages == []


def test_reklama5_malformed_listing_id_is_seeded_before_later_valid_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = _feed()
    sender = FakeSender([True])
    get = RecordingGet(
        [
            StubResponse(
                search_page(
                    1,
                    [Reklama5Card(ad_id="123", title="").html()],
                    page_links=[],
                    result_count=1,
                ),
            ),
            StubResponse(
                search_page(
                    1,
                    [Reklama5Card(ad_id="123").html()],
                    page_links=[],
                    result_count=1,
                ),
            ),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    with DeliveryStore(tmp_path / "state.db") as store:
        app = _app(store, sender, Reklama5Strategy(clock=lambda: FIXED_NOW))

        app.process_feed(feed)
        app.process_feed(feed)

        assert store.has_delivered(feed.id, "123")
    assert sender.messages == []


def test_reklama5_shutdown_during_final_page_does_not_initialize_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = _feed()
    sender = FakeSender([])
    get = RecordingGet(
        [
            StubResponse(
                search_page(
                    1,
                    [Reklama5Card(ad_id="123").html()],
                    page_links=[],
                    result_count=1,
                ),
            ),
        ],
    )
    shutdown_checks = iter((False, True))
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)
    strategy = Reklama5Strategy(
        clock=lambda: FIXED_NOW,
        is_shutdown_requested=lambda: next(shutdown_checks),
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        app = _app(store, sender, strategy)

        with pytest.raises(FeedFetchInterruptedError):
            app.process_feed(feed)

        assert not store.is_feed_initialized(feed.id)
    assert sender.messages == []


def test_reklama5_empty_first_fetch_initializes_and_first_later_listing_is_sent(
    tmp_path: Path,
) -> None:
    feed = _feed()
    sender = FakeSender([True])

    with DeliveryStore(tmp_path / "state.db") as store:
        app = _app(store, sender, SequencedReklama5Strategy([[], [_listing()]]))

        app.process_feed(feed)
        app.process_feed(feed)

        assert store.is_feed_initialized(feed.id)
        assert store.has_delivered(feed.id, "123")
    assert [message.entry.title for message in sender.messages] == ["Listing"]


@pytest.mark.parametrize("existing_state", ["seeded", "delivered"])
def test_reklama5_same_id_changes_remain_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_state: str,
) -> None:
    feed = _feed()
    original = _listing()
    changed = replace(
        original,
        title="Changed listing",
        price="200 ден.",
        activity_at=FIXED_NOW.replace(hour=13),
    )
    batches = [[original], [changed]] if existing_state == "seeded" else [[], [original], [changed]]
    sender = FakeSender([] if existing_state == "seeded" else [True])
    mark_calls: list[tuple[str, EntryId]] = []

    with DeliveryStore(tmp_path / "state.db") as store:
        original_mark_delivered = store.mark_delivered

        def record_mark_delivered(feed_id: str, entry_id: EntryId) -> None:
            mark_calls.append((feed_id, entry_id))
            original_mark_delivered(feed_id, entry_id)

        monkeypatch.setattr(store, "mark_delivered", record_mark_delivered)
        app = _app(store, sender, SequencedReklama5Strategy(batches))
        app.process_feed(feed)
        if existing_state == "delivered":
            app.process_feed(feed)
            sender.messages.clear()

        app.process_feed(feed)

        assert store.count_delivered(feed.id) == 1
    assert sender.messages == []
    assert mark_calls == ([] if existing_state == "seeded" else [(feed.id, EntryId("123"))])
