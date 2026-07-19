import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from configuration import FeedConfig
from delivery_store import DeliveryStore


def make_feed(feed_id: str, url: str) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        url=url,
        webhook=f"https://discord.test/{feed_id}",
    )


def test_delivery_store_keeps_feed_namespaces_separate(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"

    # When
    with DeliveryStore(database_path, tmp_path / "state.json", ()) as store:
        store.mark_delivered("feed-a", "entry-1")
        store.mark_delivered("feed-a", "entry-1")

        # Then
        assert store.has_delivered("feed-a", "entry-1")
        assert not store.has_delivered("feed-b", "entry-1")


def test_delivery_store_persists_after_reopen(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    legacy_path = tmp_path / "state.json"
    with DeliveryStore(database_path, legacy_path, ()) as store:
        store.mark_delivered("feed-a", "entry-1")

    # When
    with DeliveryStore(database_path, legacy_path, ()) as reopened_store:
        delivered = reopened_store.has_delivered("feed-a", "entry-1")

    # Then
    assert delivered


def test_delivery_store_migrates_legacy_url_state_to_each_feed_id(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    legacy_path = tmp_path / "state.json"
    legacy_path.write_text(
        '{"feeds":{"https://example.test/feed.xml":{"processed_ids":["one","two"]}}}',
    )
    feeds = (
        make_feed("primary", "https://example.test/feed.xml"),
        make_feed("secondary", "https://example.test/feed.xml"),
    )

    # When
    with DeliveryStore(database_path, legacy_path, feeds) as store:
        primary_delivered = store.has_delivered("primary", "one")
        secondary_delivered = store.has_delivered("secondary", "two")

    # Then
    assert primary_delivered
    assert secondary_delivered
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_delivery_store_maps_staged_legacy_state_to_feeds_configured_later(
    tmp_path: Path,
) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    legacy_path = tmp_path / "state.json"
    legacy_path.write_text(
        '{"feeds":{"https://example.test/feed.xml":{"processed_ids":["one"]}}}',
    )

    with DeliveryStore(database_path, legacy_path, ()):
        pass
    legacy_path.unlink()

    # When
    feeds = (make_feed("later", "https://example.test/feed.xml"),)
    with DeliveryStore(database_path, legacy_path, feeds) as store:
        delivered = store.has_delivered("later", "one")

    # Then
    assert delivered


@pytest.mark.parametrize(
    "legacy_contents",
    [b"{invalid", b"\xff"],
    ids=["invalid-json", "invalid-utf8"],
)
def test_delivery_store_upgrades_v1_from_delivered_rows_when_legacy_is_invalid(
    tmp_path: Path,
    legacy_contents: bytes,
) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    legacy_path = tmp_path / "state.json"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE delivered_entries ("
            "feed_id TEXT NOT NULL, "
            "entry_id TEXT NOT NULL, "
            "delivered_at INTEGER NOT NULL DEFAULT (unixepoch()), "
            "PRIMARY KEY (feed_id, entry_id)"
            ") WITHOUT ROWID",
        )
        connection.execute(
            "INSERT INTO delivered_entries (feed_id, entry_id) VALUES (?, ?)",
            ("existing", "one"),
        )
        connection.execute("PRAGMA user_version = 1")
    legacy_path.write_bytes(legacy_contents)
    feeds = (
        make_feed("existing", "https://example.test/feed.xml"),
        make_feed("later", "https://example.test/feed.xml"),
    )

    # When
    with DeliveryStore(database_path, legacy_path, feeds) as store:
        later_delivered = store.has_delivered("later", "one")

    # Then
    assert later_delivered
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


def test_delivery_store_rolls_back_invalid_legacy_migration(tmp_path: Path) -> None:
    # Given
    database_path = tmp_path / "rss2discord.db"
    legacy_path = tmp_path / "state.json"
    legacy_path.write_text(
        '{"feeds":{"https://example.test/feed.xml":{"processed_ids":[1]}}}',
    )
    feeds = (make_feed("primary", "https://example.test/feed.xml"),)

    # When / Then
    with pytest.raises(ValidationError):
        DeliveryStore(database_path, legacy_path, feeds)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
