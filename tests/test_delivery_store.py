from pathlib import Path

from rss2discord.delivery_store import DeliveryStore


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


def test_delivery_store_seeds_and_persists_feed_initialization(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"

    # When
    with DeliveryStore(database_path) as store:
        initialized_before_seed = store.is_feed_initialized("anhoch")
        seeded = store.seed_feed("anhoch", ("product-1", "product-2", "product-1"))
        reseeded = store.seed_feed("anhoch", ("product-3",))

        # Then
        assert not initialized_before_seed
        assert seeded
        assert not reseeded
        assert store.is_feed_initialized("anhoch")
        assert store.has_delivered("anhoch", "product-1")
        assert store.has_delivered("anhoch", "product-2")
        assert not store.has_delivered("anhoch", "product-3")
        assert not store.is_feed_initialized("other-feed")

    with DeliveryStore(database_path) as reopened_store:
        assert reopened_store.is_feed_initialized("anhoch")
        assert reopened_store.has_delivered("anhoch", "product-1")
