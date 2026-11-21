import json
import time
from pathlib import Path

import pytest

from rss2discord.admin import main
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import HealthUpdate

NOW = int(time.time())


def _health(
    store: DeliveryStore,
    feed_id: str,
    state: str,
    cause: str | None = None,
) -> None:
    store.record_health(
        HealthUpdate(
            feed_id=feed_id,
            job_kind="ordinary",
            state=state,
            cause=cause,
            attempted_at=NOW,
            success=state == "healthy",
            nonempty=state == "healthy",
            item_count=1 if state == "healthy" else 0,
        ),
    )


def test_admin_summary_prints_counts_and_problems(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "healthy-feed", "healthy")
        _health(store, "failed-feed", "failed", "HTTPError")

    assert main(["--database", str(database), "summary"]) == 0

    output = capsys.readouterr().out
    assert "2 feeds: 1 healthy, 0 empty, 0 stale, 1 failed, 0 blocked" in output
    assert "`failed-feed` ordinary **failed** (HTTPError)" in output


def test_admin_summary_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "blocked-feed", "blocked", "BotChallenge")

    assert main(["--database", str(database), "summary", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["total_feeds"] == 1
    assert report["blocked"] == 1
    assert report["problems"][0]["feed_id"] == "blocked-feed"
