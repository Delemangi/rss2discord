"""Discovery digest and uncapped delivery evidence."""

import csv
import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult, SleepCallback
from rss2discord.discord.discovery_digest import build_discovery_digest
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData
from rss2discord.runtime.scheduler import JobOutcome
from tests.app_helpers import (
    FakeAdapter,
    FakeEntry,
    FakeSender,
    FakeStrategy,
    make_app,
    make_entry,
    make_feed,
)


def _entries(count: int) -> list[FakeEntry]:
    return [make_entry(str(index)) for index in range(count)]


def test_twenty_five_new_entries_send_cards_and_mark_all(tmp_path: Path) -> None:
    feed = make_feed("feed")
    sender = FakeSender([True] * 25)
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy(_entries(25)), (feed,))
        app.process_feed(feed)
        assert len(sender.messages) == 25
        assert store.count_delivered(feed.id) == 25
        assert all(store.has_delivered(feed.id, str(index)) for index in range(25))


def test_twenty_six_new_entries_send_one_digest_and_mark_all(tmp_path: Path) -> None:
    feed = make_feed("feed")
    sender = FakeSender([True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy(_entries(26)), (feed,))
        app.process_feed(feed)
        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.title == "New items"
        assert len(message.files) == 2
        assert len(message.components or ()) == 4
        assert store.count_delivered(feed.id) == 26


def test_digest_html_and_csv_contain_every_entry_with_escaping(
    tmp_path: Path,
) -> None:
    entries = [
        (
            f"id-{index}",
            EntryData(
                title=f"Title {index} <script>alert(1)</script>",
                link=f"https://example.test/item/{index}?a=1&b=2",
                description="line one\nline two",
                author="=SUM(A1:A2)" if index == 0 else f"Author {index}",
                timestamp="2026-07-19T12:00:00+00:00",
                categories=("Cats | Dogs", "Other"),
            ),
        )
        for index in range(26)
    ]
    feed = make_feed("feed")
    message = build_discovery_digest(feed, entries, "Source")
    html = message.files[0].content.decode()
    csv_text = message.files[1].content.decode()

    assert 'type="search"' in html
    assert '<th data-sort="text">Title</th>' in html
    assert html.count("<tr data-search=") == 26
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "https://example.test/item/0?a=1&amp;b=2" in html
    for index in range(26):
        assert f"Title {index}" in html

    rows = list(csv.reader(io.StringIO(csv_text)))
    assert len(rows) == 27
    assert rows[0] == [
        "feed",
        "entry_id",
        "title",
        "link",
        "author",
        "published",
        "categories",
        "description",
    ]
    assert rows[1][4] == "'=SUM(A1:A2)"
    assert rows[1][7] == "line one\nline two"
    assert all(f"id-{index}" in csv_text for index in range(26))


def test_discovery_count_placeholder_in_title_survives() -> None:
    entry = make_entry("count")
    entry = replace(entry, data=replace(entry.data, title="{count} bargain"))
    message = build_discovery_digest(
        make_feed("feed"),
        [(entry.id, entry.data)],
        "Source",
    )
    html = message.files[0].content.decode()
    assert "{count} bargain" in html
    assert '<span class="note">1 rows</span>' in html


def test_digest_does_not_download_images(tmp_path: Path) -> None:
    entries = [
        (
            str(index),
            replace(
                make_entry(str(index)).data,
                image_url=f"https://images.example.test/{index}.png",
            ),
        )
        for index in range(26)
    ]
    message = build_discovery_digest(make_feed("feed"), entries, "Source")
    assert message.entry.image_url is None
    assert "https://images.example.test" not in str(message.components)


def test_failed_digest_leaves_entries_unmarked_then_retries(tmp_path: Path) -> None:
    feed = make_feed("feed")
    sender = FakeSender([False, True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy(_entries(26)), (feed,))
        app.process_feed(feed)
        assert len(sender.messages) == 1
        assert store.count_delivered(feed.id) == 0
        health = store.list_health(feed.id)[0]
        assert (health.state, health.cause) == ("failed", "DeliveryRetryPending")
        app.process_feed(feed)
        assert len(sender.messages) == 2
        assert store.count_delivered(feed.id) == 26
        assert store.list_health(feed.id)[0].state == "healthy"


class InterruptingSender(FakeSender):
    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        del sleep
        self.messages.append(message)
        return DiscordDeliveryResult.INTERRUPTED


def test_failed_single_card_records_failure_and_retries(tmp_path: Path) -> None:
    feed = make_feed("feed")
    sender = FakeSender([False, True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy(_entries(1)), (feed,))
        app.process_feed(feed)
        health = store.list_health(feed.id)[0]
        assert (health.state, health.cause) == ("failed", "DeliveryRetryPending")
        assert store.count_delivered(feed.id) == 0
        app.process_feed(feed)
        assert store.has_delivered(feed.id, "0")
        assert store.list_health(feed.id)[0].state == "healthy"


def test_interrupted_digest_leaves_entries_unmarked(tmp_path: Path) -> None:
    feed = make_feed("feed")
    sender = InterruptingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy(_entries(26)), (feed,))
        assert app.process_feed(feed) is JobOutcome.ATTEMPTED
        assert len(sender.messages) == 1
        assert store.count_delivered(feed.id) == 0
        assert store.list_health(feed.id) == ()


def test_large_batch_has_no_cap_error(tmp_path: Path) -> None:
    feed = make_feed("feed")
    sender = FakeSender([True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy(_entries(500)), (feed,))
        app.process_feed(feed)
        assert len(sender.messages) == 1
        assert store.count_delivered(feed.id) == 500


def test_missing_and_too_old_timestamps_are_still_skipped(tmp_path: Path) -> None:
    feed = make_feed("feed")
    missing = make_entry("missing")
    missing = replace(missing, data=replace(missing.data, timestamp=None))
    old = make_entry("old")
    old = replace(
        old,
        data=replace(old.data, timestamp="2000-01-01T00:00:00+00:00"),
    )
    fresh = make_entry("fresh")
    fresh = replace(
        fresh,
        data=replace(fresh.data, timestamp=datetime.now(UTC).isoformat()),
    )
    sender = FakeSender([True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(
            AppConfig(max_post_age_days=1, delay_between_posts=0, feeds=(feed,)),
            store,
            sender,
        )
        app._strategies["rss"] = FakeStrategy([missing, old, fresh])
        app.process_feed(feed)
        assert [message.entry.title for message in sender.messages] == ["fresh"]
        assert store.has_delivered(feed.id, "fresh")
        assert not store.has_delivered(feed.id, "missing")
        assert not store.has_delivered(feed.id, "old")


def test_enrichment_limit_is_preserved_for_small_batches(tmp_path: Path) -> None:
    feed = make_feed("feed").model_copy(update={"adapter": "hackernews"})
    adapter = FakeAdapter()
    sender = FakeSender([True] * 6)
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy(_entries(6)), (feed,))
        app._adapters["hackernews"] = adapter
        app.process_feed(feed)
        assert len(adapter.entries) == 5
        assert [message.entry.author for message in sender.messages[:5]] == [
            "Adapted Author",
        ] * 5
        assert sender.messages[5].entry.author == "Author"


def test_mark_delivered_many_is_idempotent(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.mark_delivered_many("feed", ("a", "b"))
        store.mark_delivered_many("feed", ("a", "b"))
        assert store.count_delivered("feed") == 2
        assert store.has_delivered("feed", "a")
        assert store.has_delivered("feed", "b")
