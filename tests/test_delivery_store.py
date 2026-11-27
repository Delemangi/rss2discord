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


def test_delivery_store_loads_one_feeds_delivered_ids(tmp_path: Path) -> None:
    # Given delivered entries in two feeds
    database_path = tmp_path / "rss2discord.db"

    with DeliveryStore(database_path) as store:
        store.mark_delivered_many("feed-a", ("1", "2"))
        store.mark_delivered("feed-b", "1")

        # When / Then batch loading is scoped to one feed
        assert store.load_delivered_entry_ids("feed-a") == {"1", "2"}
        assert store.load_delivered_entry_ids("feed-b") == {"1"}


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


def test_delivery_store_reseeds_when_entry_id_scheme_changes(
    tmp_path: Path,
) -> None:
    # Given a feed initialized with the original entry-ID scheme
    database_path = tmp_path / "rss2discord.db"

    with DeliveryStore(database_path) as store:
        assert store.seed_feed("auction", ("server-1:100",), entry_id_scheme=1)
        assert not store.seed_feed("auction", ("server-2:100",), entry_id_scheme=1)

        # When the provider switches to a new scheme
        reseeded = store.seed_feed(
            "auction",
            ("server-1:95", "server-2:95"),
            entry_id_scheme=2,
        )

        # Then the current window is baselined silently once
        assert reseeded
        assert store.has_delivered("auction", "server-1:95")
        assert store.has_delivered("auction", "server-2:95")
        assert not store.seed_feed(
            "auction",
            ("server-3:95",),
            entry_id_scheme=2,
        )
