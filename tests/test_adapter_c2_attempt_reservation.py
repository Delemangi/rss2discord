from pathlib import Path

from rss2discord.delivery_store import DeliveryStore


def test_confirmation_rotation_is_durable_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    ids = ("c", "a", "b", "d")
    with DeliveryStore(path) as store:
        for _ in range(2):
            first = store.select_price_confirmation_subset(
                feed_id="feed",
                product_ids=ids,
                limit=2,
            )
            second = store.select_price_confirmation_subset(
                feed_id="feed",
                product_ids=ids,
                limit=2,
            )
            assert first == ("a", "b")
            assert second == ("c", "d")
    with DeliveryStore(path) as store:
        assert store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=ids,
            limit=2,
        ) == ("a", "b")
