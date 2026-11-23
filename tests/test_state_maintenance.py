import logging
import time
from pathlib import Path

import pytest

from rss2discord.delivery_store import STATE_JOURNAL_SIZE_LIMIT_BYTES, DeliveryStore
from rss2discord.main import _prune_state
from rss2discord.models import HealthUpdate


def _health(feed_id: str) -> HealthUpdate:
    return HealthUpdate(
        feed_id=feed_id,
        job_kind="ordinary",
        state="healthy",
        cause=None,
        attempted_at=1,
        success=True,
        nonempty=True,
        item_count=1,
    )


def test_writable_store_caps_the_wal_size(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        limit = store._connection.execute("PRAGMA journal_size_limit").fetchone()

    assert limit == (STATE_JOURNAL_SIZE_LIMIT_BYTES,)


def test_prune_unconfigured_health_keeps_current_feeds(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(_health("current"))
        store.record_health(_health("removed"))

        removed = store.prune_unconfigured_health({"current"})

        assert removed == 1
        assert [record.feed_id for record in store.list_health()] == ["current"]


def test_prune_state_removes_old_delivered_rows(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    cutoff = int(time.time()) - 91 * 86_400
    with DeliveryStore(tmp_path / "state.db") as store:
        store._connection.execute(
            "INSERT INTO delivered_entries (feed_id, entry_id, delivered_at) "
            "VALUES (?, ?, ?)",
            ("feed", "old", cutoff),
        )
        store._connection.commit()

        _prune_state(store, 90)

        assert not store.has_delivered("feed", "old")

    assert "Pruned state older than 90 days" in caplog.text
