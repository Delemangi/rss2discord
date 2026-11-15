"""Read-only delivery assertions shared by tests."""

import sqlite3

from rss2discord.delivery_store import DeliveryStore


def count_delivered(store: DeliveryStore, feed_id: str) -> int:
    """Count confirmed deliveries for one feed from its persisted state."""
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM delivered_entries WHERE feed_id = ?",
            (feed_id,),
        ).fetchone()
    return 0 if row is None else int(row[0])
