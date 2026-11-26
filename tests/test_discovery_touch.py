"""Discovery refreshes delivered entries still present in the source."""

import time
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from tests.app_helpers import FakeSender, FakeStrategy, make_app, make_entry, make_feed

OLD = int(time.time()) - 120 * 86_400


def test_seen_delivered_entry_is_refreshed_for_retention(tmp_path: Path) -> None:
    # Given an entry delivered longer ago than the touch threshold
    feed = make_feed("catalog")
    strategy = FakeStrategy([make_entry("still-listed")])
    sender = FakeSender([])

    with DeliveryStore(tmp_path / "state.db") as store:
        store.mark_delivered(feed.id, "still-listed")
        with store._connection:
            store._connection.execute(
                "UPDATE delivered_entries SET delivered_at = ?",
                (OLD,),
            )
        app = make_app(store, sender, strategy, (feed,))

        # When the source still lists it
        app.process_feed(feed)

        # Then its retention age is refreshed and it stays delivered
        refreshed = store._connection.execute(
            "SELECT delivered_at FROM delivered_entries "
            "WHERE feed_id = ? AND entry_id = ?",
            (feed.id, "still-listed"),
        ).fetchone()
        assert refreshed is not None
        assert int(refreshed[0]) > OLD
        assert sender.messages == []
