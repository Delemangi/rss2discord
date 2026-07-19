from pathlib import Path

from delivery_store import DeliveryStore


def test_delivery_store_keeps_feed_namespaces_separate(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"

    # When
    with DeliveryStore(database_path) as store:
        store.mark_delivered("feed-a", "entry-1")
        store.mark_delivered("feed-a", "entry-1")

        # Then
        assert store.has_delivered("feed-a", "entry-1")
        assert not store.has_delivered("feed-b", "entry-1")


def test_delivery_store_persists_after_reopen(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    with DeliveryStore(database_path) as store:
        store.mark_delivered("feed-a", "entry-1")

    # When
    with DeliveryStore(database_path) as reopened_store:
        delivered = reopened_store.has_delivered("feed-a", "entry-1")

    # Then
    assert delivered
