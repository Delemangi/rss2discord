import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.providers.anhoch.prices import AnhochPriceMonitorDependencies
from rss2discord.recovery_models import HealthUpdate
from rss2discord.scheduler import JobTiming
from tests.app_helpers import (
    FakeEntry,
    FakeSender,
    FakeStrategy,
    make_app,
    make_entry,
    make_feed,
)
from tests.runtime_helpers import FakeClock, RecordingMonitor


class CompleteCatalogStrategy(FakeStrategy):
    allow_missing_timestamp_after_complete_baseline = True
    seed_existing_on_first_fetch = True
    max_new_entries_per_fetch = 1
    max_delivery_history = 2


class CountingStrategy(FakeStrategy):
    def __init__(self, error: FeedFetchError | None = None) -> None:
        super().__init__([])
        self.calls = 0
        self.error = error

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return super().fetch_entries(url)


def _undated(entry_id: str) -> FakeEntry:
    entry = make_entry(entry_id)
    return replace(entry, data=replace(entry.data, timestamp=None))


def _catalog_app(
    store: DeliveryStore,
    entries: list[FakeEntry],
    sender: FakeSender,
) -> tuple[RSSToDiscord, FeedConfig, CompleteCatalogStrategy]:
    feed = FeedConfig(
        id="cccenter",
        strategy="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/hook",
    )
    strategy = CompleteCatalogStrategy(entries)
    app = RSSToDiscord(
        AppConfig(feeds=(feed,), delay_between_posts=0, max_post_age_days=1),
        store,
        sender,
    )
    app._strategies["cccenter"] = strategy
    return app, feed, strategy


def test_fresh_catalog_baseline_is_not_delivery_and_allows_only_new_undated_ids(
    tmp_path: Path,
) -> None:
    sender = FakeSender([True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app, feed, strategy = _catalog_app(
            store,
            [_undated(str(n)) for n in range(5)],
            sender,
        )
        app.process_feed(feed)
        assert store.has_complete_baseline(feed.id)
        health = store.list_health(feed.id)[0]
        assert (health.state, health.total_attempts, health.total_successes) == (
            "healthy",
            1,
            1,
        )
        assert store.has_baselined(feed.id, "0")
        assert store.count_delivered(feed.id) == 0
        assert sender.messages == []
        strategy.entries.append(_undated("new"))
        app.process_feed(feed)
        app.process_feed(feed)
        assert store.count_delivered(feed.id) == 1
        assert store.has_delivered(feed.id, "new")
        assert not store.has_delivered(feed.id, "0")
        assert len(sender.messages) == 1


def test_legacy_catalog_waits_for_exact_manual_baseline_approval(
    tmp_path: Path,
) -> None:
    sender = FakeSender([True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app, feed, strategy = _catalog_app(
            store,
            [_undated("legacy"), _undated("missed")],
            sender,
        )
        store.seed_feed(feed.id, ["legacy"])
        app.process_feed(feed)
        candidate = store.load_baseline_candidate(feed.id)
        assert candidate is not None
        assert not store.has_complete_baseline(feed.id)
        assert store.list_health(feed.id)[0].state == "recovery_required"
        app.process_feed(feed)
        assert store.load_baseline_candidate(feed.id) == candidate
        assert sender.messages == []
        store.approve_feed_baseline_candidate(
            feed_id=feed.id,
            fingerprint=candidate.fingerprint,
            reason="Reviewed complete inventory",
        )
        app.process_feed(feed)
        strategy.entries.append(_undated("new"))
        app.process_feed(feed)
        assert [message.entry.title for message in sender.messages] == ["new"]
        assert store.count_delivered(feed.id) == 2
        assert not store.has_delivered(feed.id, "missed")
        assert store.has_baselined(feed.id, "missed")


def test_baseline_does_not_disable_age_filter_for_dated_catalog_entries(
    tmp_path: Path,
) -> None:
    sender = FakeSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        app, feed, strategy = _catalog_app(store, [_undated("old")], sender)
        app.process_feed(feed)
        old_entry = make_entry("dated")
        strategy.entries.append(
            replace(
                old_entry,
                data=replace(old_entry.data, timestamp="2000-01-01T00:00:00+00:00"),
            ),
        )
        app.process_feed(feed)
        assert sender.messages == []


def test_rss_missing_timestamp_remains_rejected_even_with_explicit_baseline(
    tmp_path: Path,
) -> None:
    feed = make_feed("rss")
    sender = FakeSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        store.initialize_feed_with_baseline(
            feed_id=feed.id,
            entry_ids=["old"],
            reason="Existing inventory",
        )
        app = RSSToDiscord(AppConfig(feeds=(feed,), max_post_age_days=1), store, sender)
        app._strategies["rss"] = FakeStrategy([_undated("new")])
        app.process_feed(feed)
        assert sender.messages == []


def test_failed_catalog_does_not_create_baseline_candidate(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        app, feed, _strategy = _catalog_app(store, [], FakeSender([]))
        store.seed_feed(feed.id, ["legacy"])
        app._strategies["cccenter"] = CountingStrategy(
            FeedFetchError("CCCenter", "IncompletePage"),
        )
        app._process_feed_safely(feed)
        assert store.load_baseline_candidate(feed.id) is None
        assert store.list_health(feed.id)[0].state == "failed"


def test_blocked_origin_skips_peer_ordinary_and_price_jobs_across_restart(
    tmp_path: Path,
) -> None:
    blocked = make_feed("blocked")
    peer = FeedConfig(
        id="peer",
        strategy="anhoch",
        url="https://example.test:443/other?secret=hidden",
        webhook="https://discord.example.test/hook",
        price_check_interval=5,
    )
    unrelated = FeedConfig(
        id="unrelated",
        url="https://other.example.test/feed",
        webhook="https://discord.example.test/hook",
    )
    feeds = (blocked, peer, unrelated)
    now = int(time.time())
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        app = RSSToDiscord(AppConfig(feeds=feeds), store, FakeSender([]))
        blocked_strategy = CountingStrategy(FeedFetchError("RSS", "BotChallenge"))
        app._strategies["rss"] = blocked_strategy
        app._process_feed_safely(blocked)
        deadline = store.get_blocked_until(blocked.id)
        assert deadline is not None
        assert deadline >= now + 21600
    with DeliveryStore(path) as store:
        app = RSSToDiscord(AppConfig(feeds=feeds), store, FakeSender([]))
        strategy = CountingStrategy()
        app._strategies["anhoch"] = strategy
        app._strategies["rss"] = strategy
        app.process_feed(peer)
        assert strategy.calls == 0
        events: list[tuple[str, float]] = []

        def factory(
            feed: FeedConfig,
            dependencies: AnhochPriceMonitorDependencies,
        ) -> RecordingMonitor:
            del dependencies
            return RecordingMonitor(feed.id, events, FakeClock(1))

        jobs = build_price_jobs(
            AppConfig(feeds=feeds),
            PriceJobDependencies(
                store,
                FakeSender([]),
                lambda _: True,
                0,
                lambda: False,
            ),
            anhoch_monitor_factory=factory,
        )
        jobs[0].run()
        assert events == []
        assert store.get_blocked_until(blocked.id) == deadline
        assert store.list_health(blocked.id)[0].total_attempts == 1
        app.process_feed(unrelated)
        assert strategy.calls == 1


def test_expired_block_allows_real_probe_and_healthy_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = make_feed("feed")
    now = int(time.time())
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(
            HealthUpdate(
                feed.id,
                "ordinary",
                "blocked",
                "BotChallenge",
                now,
                False,
                False,
                0,
            ),
        )
        monkeypatch.setattr("rss2discord.app.time.time", lambda: now + 21601)
        strategy = CountingStrategy()
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, FakeSender([]))
        app._strategies["rss"] = strategy
        app.process_feed(feed)
        assert strategy.calls == 1
        assert store.get_blocked_until(feed.id) is None
        assert store.list_health(feed.id)[0].state == "empty"


def test_health_error_payload_and_logs_do_not_expose_exception_secret(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    feed = make_feed("bad")
    caplog.set_level(logging.INFO)
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, FakeSender([]))
        app._strategies["rss"] = CountingStrategy(
            FeedFetchError("RSS", "https://secret.example/token"),
        )
        app._process_feed_safely(feed)
        assert store.list_health(feed.id)[0].cause == "FetchError"
        assert "secret.example" not in caplog.text


def test_successful_price_return_and_timing_do_not_erase_quarantine(
    tmp_path: Path,
) -> None:
    feed = FeedConfig(
        id="prices",
        strategy="anhoch",
        url="https://example.test/catalog",
        webhook="https://discord.example.test/hook",
        price_check_interval=5,
    )
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(
            HealthUpdate(
                feed.id,
                "price",
                "quarantined",
                "ApprovalRequired",
                int(time.time()),
                True,
                True,
                200,
            ),
        )

        def factory(
            configured: FeedConfig,
            dependencies: AnhochPriceMonitorDependencies,
        ) -> RecordingMonitor:
            del dependencies
            return RecordingMonitor(configured.id, [], FakeClock(1))

        jobs = build_price_jobs(
            AppConfig(feeds=(feed,)),
            PriceJobDependencies(
                store,
                FakeSender([]),
                lambda _: True,
                0,
                lambda: False,
            ),
            anhoch_monitor_factory=factory,
        )
        jobs[0].run()
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, FakeSender([]))
        app._record_job_timing(JobTiming(feed.id, "price", 1, 4, 6))
        record = store.list_health(feed.id)[0]
        assert record.state == "quarantined"
        assert record.cause == "ApprovalRequired"
        assert record.item_count == 200
        assert record.duration_ms == 2000
        assert record.scheduler_lag_ms == 3000


def test_price_challenge_blocks_ordinary_before_fetch(tmp_path: Path) -> None:
    feed = FeedConfig(
        id="prices",
        strategy="anhoch",
        url="https://example.test/catalog",
        webhook="https://discord.example.test/hook",
        price_check_interval=5,
    )

    class BlockedMonitor:
        def scan(self) -> None:
            raise FeedFetchError("Anhoch", "AccessChallenge", status_code=403)

    def factory(
        configured: FeedConfig,
        dependencies: AnhochPriceMonitorDependencies,
    ) -> BlockedMonitor:
        del configured, dependencies
        return BlockedMonitor()

    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(feed,)),
            PriceJobDependencies(
                store,
                FakeSender([]),
                lambda _: True,
                0,
                lambda: False,
            ),
            anhoch_monitor_factory=factory,
        )
        jobs[0].run()
        deadline = store.get_blocked_until(feed.id)
        assert deadline is not None
        jobs[0].run()
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, FakeSender([]))
        strategy = CountingStrategy()
        app._strategies["anhoch"] = strategy
        app.process_feed(feed)
        assert strategy.calls == 0
        assert store.list_health(feed.id)[0].total_attempts == 1
        assert store.get_blocked_until(feed.id) == deadline


def test_quiet_feed_health_tracks_nonempty_fetch_without_requiring_delivery(
    tmp_path: Path,
) -> None:
    feed = make_feed("quiet")
    sender = FakeSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(feeds=(feed,), max_post_age_days=1), store, sender)
        strategy = FakeStrategy([])
        app._strategies["rss"] = strategy
        app.process_feed(feed)
        assert store.list_health(feed.id)[0].state == "empty"
        old_entry = make_entry("old")
        strategy.entries = [
            replace(
                old_entry,
                data=replace(old_entry.data, timestamp="2000-01-01T00:00:00+00:00"),
            ),
        ]
        app.process_feed(feed)
        record = store.list_health(feed.id)[0]
        assert record.state == "healthy"
        assert record.total_successes == 2
        assert record.total_nonempty == 1
        assert record.item_count == 1
        assert sender.messages == []


def test_expired_blocked_probe_restarts_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = make_feed("probe")
    now = int(time.time())
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(
            HealthUpdate(
                feed.id,
                "ordinary",
                "blocked",
                "BotChallenge",
                now,
                False,
                False,
                0,
            ),
        )
        monkeypatch.setattr("rss2discord.app.time.time", lambda: now + 21601)
        strategy = CountingStrategy(FeedFetchError("RSS", "BotChallenge"))
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, FakeSender([]))
        app._strategies["rss"] = strategy
        app._process_feed_safely(feed)
        assert strategy.calls == 1
        assert store.get_blocked_until(feed.id) == now + 43201
        app._process_feed_safely(feed)
        assert strategy.calls == 1


@pytest.mark.parametrize("limit", ["max_new_entries_per_fetch", "max_delivery_history"])
def test_repeated_delivery_limit_failures_record_one_outcome_without_false_recovery(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    limit: str,
) -> None:
    caplog.set_level(logging.INFO)
    feed = make_feed("limited")
    strategy = FakeStrategy([make_entry("a"), make_entry("b")])
    setattr(strategy, limit, 1)
    sender = FakeSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, strategy, (feed,))
        for attempt in range(1, 4):
            app._process_feed_safely(feed)
            health = store.list_health(feed.id)[0]
            assert health.state == "failed"
            assert health.total_attempts == attempt
            assert health.total_successes == 0
            assert health.consecutive_failures == attempt
        assert sender.messages == []
        assert "recovered=True" not in caplog.text
        assert caplog.text.count("health=failed") == 1

        strategy.entries.clear()
        app._process_feed_safely(feed)
        health = store.list_health(feed.id)[0]
        assert health.state == "empty"
        assert health.total_attempts == 4
        assert health.total_successes == 1
        assert health.consecutive_failures == 0
        assert caplog.text.count("recovered=True") == 1


def test_ordinary_delivery_exception_records_only_failure_then_real_recovery(
    tmp_path: Path,
) -> None:
    feed = make_feed("delivery")
    sender = FakeSender([RuntimeError("delivery failed"), True])
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, sender, FakeStrategy([make_entry("a")]), (feed,))
        app._process_feed_safely(feed)
        health = store.list_health(feed.id)[0]
        assert (health.state, health.total_attempts, health.total_successes) == (
            "failed",
            1,
            0,
        )
        assert not store.has_delivered(feed.id, "a")
        app._process_feed_safely(feed)
        health = store.list_health(feed.id)[0]
        assert (health.state, health.total_attempts, health.total_successes) == (
            "healthy",
            2,
            1,
        )
        assert store.has_delivered(feed.id, "a")


def test_unsuccessful_send_is_not_a_fetch_failure(tmp_path: Path) -> None:
    feed = make_feed("delivery")
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(
            store,
            FakeSender([False]),
            FakeStrategy([make_entry("a")]),
            (feed,),
        )
        app._process_feed_safely(feed)
        health = store.list_health(feed.id)[0]
        assert (health.state, health.total_attempts, health.total_successes) == (
            "healthy",
            1,
            1,
        )
        assert not store.has_delivered(feed.id, "a")


@pytest.mark.parametrize("quiet_case", ["seed", "handled", "old", "empty_seed"])
def test_quiet_ordinary_processing_records_one_success(
    tmp_path: Path,
    quiet_case: str,
) -> None:
    feed = make_feed(
        "quiet",
        seed_existing_on_first_fetch=quiet_case in {"seed", "empty_seed"},
    )
    entry = make_entry("a")
    if quiet_case == "old":
        entry = replace(
            entry,
            data=replace(entry.data, timestamp="2000-01-01T00:00:00+00:00"),
        )
    entries = [] if quiet_case == "empty_seed" else [entry]
    sender = FakeSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        if quiet_case == "handled":
            store.mark_delivered(feed.id, entry.id)
        app = RSSToDiscord(
            AppConfig(feeds=(feed,), max_post_age_days=1),
            store,
            sender,
        )
        app._strategies["rss"] = FakeStrategy(entries)
        app._process_feed_safely(feed)
        health = store.list_health(feed.id)[0]
        assert health.state == ("empty" if quiet_case == "empty_seed" else "healthy")
        assert health.total_attempts == health.total_successes == 1
        assert health.total_nonempty == bool(entries)
        assert sender.messages == []


@pytest.mark.parametrize(
    "phase",
    ["before_fetch", "after_fetch", "post_send", "persistence"],
)
def test_interrupted_ordinary_processing_does_not_publish_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    feed = make_feed("interrupted")
    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(
            store,
            FakeSender([True]),
            FakeStrategy([make_entry("a")]),
            (feed,),
        )
        if phase == "before_fetch":
            app.request_shutdown()
        elif phase == "after_fetch":

            def fetch(_url: str) -> tuple[list[Any], str]:
                app.request_shutdown()
                return [make_entry("a")], "Source"

            monkeypatch.setattr(app._strategies["rss"], "fetch_entries", fetch)
        elif phase == "persistence":
            monkeypatch.setattr(app, "_persist_delivery", lambda *_: False)
        else:

            def stop(_seconds: float) -> bool:
                app.request_shutdown()
                return False

            monkeypatch.setattr(app, "_interruptible_sleep", stop)
        app._process_feed_safely(feed)
        assert store.list_health(feed.id) == ()
