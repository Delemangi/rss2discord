"""Entry-ID scheme changes rebaseline a feed without replaying its window."""

from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from tests.app_helpers import FakeSender, FakeStrategy, make_app, make_entry, make_feed


def test_changed_entry_id_scheme_rebaselines_the_current_window(
    tmp_path: Path,
) -> None:
    # Given a feed whose stored baseline uses the previous ID scheme
    feed = make_feed("hetzner-auction")
    strategy = FakeStrategy([make_entry("10"), make_entry("20")])
    strategy.seed_existing_on_first_fetch = True
    strategy.entry_id_scheme_version = 2
    sender = FakeSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.seed_feed(feed.id, ("old-10",), entry_id_scheme=1)
        app = make_app(store, sender, strategy, (feed,))

        # When
        app.process_feed(feed)

        # Then the new IDs are baselined silently
        assert sender.messages == []
        assert store.has_delivered(feed.id, "10")
        assert store.has_delivered(feed.id, "20")
