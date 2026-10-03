import sqlite3
from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from rss2discord.recovery_models import HealthUpdate


def _update(
    *,
    feed_id: str = "feed",
    job_kind: str = "ordinary",
    state: str,
    attempted_at: int,
    success: bool,
    nonempty: bool,
    cause: str | None = None,
    item_count: int = 0,
) -> HealthUpdate:
    return HealthUpdate(
        feed_id=feed_id,
        job_kind=job_kind,
        state=state,
        cause=cause,
        attempted_at=attempted_at,
        success=success,
        nonempty=nonempty,
        item_count=item_count,
    )


def test_blocked_probe_restarts_cooldown_only_after_expiry(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(
            _update(
                state="blocked",
                cause="BotChallenge",
                attempted_at=1_000,
                success=False,
                nonempty=False,
            ),
        )
        store.record_health(
            _update(
                state="blocked",
                cause="SkippedWhileBlocked",
                attempted_at=1_100,
                success=False,
                nonempty=False,
            ),
        )
        before_expiry = store.list_health("feed")[0].blocked_until
        store.record_health(
            _update(
                state="blocked",
                cause="BotChallenge",
                attempted_at=22_601,
                success=False,
                nonempty=False,
            ),
        )
        after_expiry = store.list_health("feed")[0].blocked_until

        assert before_expiry == 22_600
        assert after_expiry == 44_201


def test_failure_counter_and_success_nonempty_timestamps_follow_outcomes(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(
            _update(
                state="healthy",
                attempted_at=100,
                success=True,
                nonempty=True,
                item_count=2,
            ),
        )
        store.record_health(
            _update(
                state="failed",
                attempted_at=110,
                success=False,
                nonempty=False,
                cause="FetchError",
            ),
        )
        store.record_health(
            _update(
                state="failed",
                attempted_at=120,
                success=False,
                nonempty=False,
                cause="FetchError",
            ),
        )
        recovered = store.record_health(
            _update(
                state="healthy",
                attempted_at=130,
                success=True,
                nonempty=False,
            ),
        )
        store.record_health(
            _update(
                state="healthy",
                attempted_at=140,
                success=True,
                nonempty=True,
                item_count=1,
            ),
        )

        record = store.list_health("feed")[0]
        assert recovered.recovered
        assert record.consecutive_failures == 0
        assert record.total_attempts == 5
        assert record.total_successes == 3
        assert record.total_nonempty == 2
        assert record.last_success_at == 140
        assert record.last_nonempty_at == 140


def test_existing_health_schema_gets_timestamp_columns_additively(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE health_state (feed_id TEXT NOT NULL, job_kind TEXT NOT NULL, "
            "state TEXT NOT NULL, cause TEXT, attempted_at INTEGER, success INTEGER NOT NULL, "
            "nonempty INTEGER NOT NULL, item_count INTEGER NOT NULL, duration_ms INTEGER, "
            "scheduler_lag_ms INTEGER, transition_at INTEGER, consecutive_failures INTEGER NOT NULL, "
            "total_attempts INTEGER NOT NULL, total_successes INTEGER NOT NULL, "
            "total_nonempty INTEGER NOT NULL, total_items INTEGER NOT NULL, blocked_until INTEGER, "
            "last_notified_at INTEGER, PRIMARY KEY(feed_id, job_kind)) WITHOUT ROWID",
        )

    with DeliveryStore(database) as store:
        columns = {
            row[1]
            for row in store._connection.execute(
                "PRAGMA table_info(health_state)",
            )
        }
        assert {"last_success_at", "last_nonempty_at"} <= columns
