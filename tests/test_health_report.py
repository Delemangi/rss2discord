import time
from decimal import Decimal
from pathlib import Path
from threading import Event

from rss2discord.delivery_store import DeliveryStore
from rss2discord.health_report import (
    MAX_SUMMARY_CHARACTERS,
    HealthSummary,
    HealthSummaryReporter,
    build_health_summary,
    format_health_summary,
)
from rss2discord.models import EntryData, HealthUpdate, PriceSnapshot
from rss2discord.price_notifications import PriceNotification

NOW = int(time.time())


def _health(
    store: DeliveryStore,
    feed_id: str,
    state: str,
    *,
    cause: str | None = None,
    job_kind: str = "ordinary",
) -> None:
    store.record_health(
        HealthUpdate(
            feed_id=feed_id,
            job_kind=job_kind,
            state=state,
            cause=cause,
            attempted_at=NOW,
            success=state == "healthy",
            nonempty=state == "healthy",
            item_count=1 if state == "healthy" else 0,
        ),
    )


def _seed_pending_notification(store: DeliveryStore) -> None:
    snapshot = PriceSnapshot(
        feed_id="pending-feed",
        product_id="1",
        amount=Decimal(90),
        formatted="90 MKD",
        currency="MKD",
    )
    store.record_price_scan(
        feed_id="pending-feed",
        group_id="group",
        observed_at=NOW,
        baseline_updates=(),
        notifications=(
            PriceNotification(
                product_id="1",
                previous=snapshot,
                current=snapshot,
                entry=EntryData(
                    title="Item",
                    link="https://example.test/1",
                    description="",
                    author="",
                    timestamp=None,
                ),
                source_title="Shop",
            ),
        ),
    )


def test_build_health_summary_counts_states_and_pending(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "healthy-feed", "healthy")
        _health(store, "empty-feed", "empty")
        _health(store, "failed-feed", "failed", cause="HTTPError")
        _health(store, "blocked-feed", "blocked", cause="BotChallenge")
        _seed_pending_notification(store)

        summary = build_health_summary(store, database)

    assert summary.total_feeds == 4
    assert summary.healthy == 1
    assert summary.empty == 1
    assert summary.failed == 1
    assert summary.blocked == 1
    assert summary.other == 0
    assert summary.pending_price_notifications == 1
    assert summary.database_bytes > 0
    assert summary.database_file_bytes > 0
    assert summary.database_bytes >= summary.database_file_bytes + summary.wal_bytes
    assert [record.feed_id for record in summary.problems] == [
        "failed-feed",
        "blocked-feed",
        "empty-feed",
    ]


def test_quiet_ordinary_scan_is_healthy_after_producing_items(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "quiet-feed", "healthy")
        _health(store, "quiet-feed", "empty")

        summary = build_health_summary(store, database)

    assert summary.total_feeds == 1
    assert summary.healthy == 1
    assert summary.empty == 0
    assert summary.problems == ()


def test_empty_price_monitor_remains_a_problem_after_producing_items(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "price-feed", "healthy", job_kind="price")
        _health(store, "price-feed", "empty", job_kind="price")

        summary = build_health_summary(store, database)

    assert summary.empty == 1
    assert [record.job_kind for record in summary.problems] == ["price"]


def test_never_populated_ordinary_feed_remains_a_problem(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "new-feed", "empty")

        summary = build_health_summary(store, database)

    assert summary.empty == 1
    assert [record.feed_id for record in summary.problems] == ["new-feed"]


def test_quiet_ordinary_scan_does_not_hide_a_failed_price_job(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "mixed-feed", "healthy")
        _health(store, "mixed-feed", "empty")
        _health(store, "mixed-feed", "failed", cause="HTTPError", job_kind="price")

        summary = build_health_summary(store, database)

    assert summary.failed == 1
    assert len(summary.problems) == 1
    assert summary.problems[0].job_kind == "price"


def test_build_health_summary_uses_worst_state_per_feed(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "mixed-feed", "healthy", job_kind="ordinary")
        _health(
            store,
            "mixed-feed",
            "failed",
            cause="HTTPError",
            job_kind="price",
        )
        summary = build_health_summary(store, database)

    assert summary.total_feeds == 1
    assert summary.healthy == 0
    assert summary.failed == 1
    assert len(summary.problems) == 1
    assert summary.problems[0].job_kind == "price"


def test_format_health_summary_reports_database_and_wal_separately() -> None:
    summary = HealthSummary(
        total_feeds=1,
        healthy=1,
        empty=0,
        stale=0,
        failed=0,
        blocked=0,
        other=0,
        pending_price_notifications=0,
        database_bytes=3_072,
        database_file_bytes=1_024,
        wal_bytes=2_048,
        problems=(),
    )

    text = format_health_summary(summary)

    assert "Database: 1.0 KB (WAL 2.0 KB)" in text


def test_format_health_summary_includes_problems_and_limits_length(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        for index in range(20):
            _health(store, f"failed-{index:02}", "failed", cause="HTTPError")
        summary = build_health_summary(store, database)

    text = format_health_summary(summary)

    assert "**rss2discord health**" in text
    assert "20 feeds: 0 healthy, 0 empty, 0 stale, 20 failed, 0 blocked" in text
    assert "`failed-00`" in text
    assert "… and 8 more" in text
    assert len(text) <= MAX_SUMMARY_CHARACTERS


def test_health_summary_reporter_sends_on_start_and_stops(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _health(store, "healthy-feed", "healthy")

    sent = Event()
    messages: list[str] = []

    def sender(text: str) -> bool:
        messages.append(text)
        sent.set()
        return True

    reporter = HealthSummaryReporter(
        webhook="https://discord.test/webhooks/id/token",
        database_path=database,
        interval=60,
        sender=sender,
    )

    reporter.start()
    assert sent.wait(2)
    reporter.stop()

    assert len(messages) == 1
    assert "**rss2discord health**" in messages[0]
    assert reporter._thread is not None
    assert not reporter._thread.is_alive()
