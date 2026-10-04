import logging
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.recovery_models import HealthUpdate
from rss2discord.scheduler import JobOutcome, SchedulerJobs
from tests.app_helpers import FakeSender, FakeStrategy, make_feed


@pytest.mark.parametrize(
    "field",
    ["refresh_interval", "delay_between_feeds", "delay_between_posts"],
)
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_cadence_rejects_nonfinite_values(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({field: value})


@pytest.mark.parametrize("gap", [0, 149, 150, 151])
def test_startup_warns_for_gap_budget_but_still_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    gap: float,
) -> None:
    feeds = (make_feed("a"), make_feed("b"))
    caplog.set_level(logging.WARNING)
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(
            AppConfig(feeds=feeds, refresh_interval=300, delay_between_feeds=gap),
            store,
            FakeSender([]),
        )
        app._strategies["rss"] = FakeStrategy([])

        def stop(_seconds: float) -> bool:
            app.request_shutdown()
            return False

        monkeypatch.setattr(app, "_interruptible_sleep", stop)
        app.run()
        assert store.list_health("a")[0].total_attempts == 1

    assert ("cadence is best-effort" in caplog.text) == (gap >= 150)


@pytest.mark.parametrize("price_interval", [7200, 1e-320, 5e-324])
def test_per_feed_intervals_set_ordinary_jobs_and_capacity_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    price_interval: float,
) -> None:
    fast = make_feed("fast").model_copy(update={"ordinary_check_interval": 300})
    slow = FeedConfig.model_validate(
        make_feed("slow").model_dump()
        | {
            "ordinary_check_interval": 3600,
            "strategy": "anhoch",
            "price_check_interval": price_interval,
        },
    )
    captured: list[SchedulerJobs] = []

    class CaptureScheduler:
        def __init__(
            self,
            jobs: SchedulerJobs,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            captured.append(jobs)

        def run(self) -> None:
            return

    monkeypatch.setattr("rss2discord.app.RuntimeScheduler", CaptureScheduler)
    caplog.set_level(logging.WARNING)
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(
            AppConfig(
                feeds=(fast, slow),
                refresh_interval=300,
                delay_between_feeds=150,
            ),
            store,
            FakeSender([]),
        )
        app.run()

    jobs = captured[0]
    assert [job.interval for job in jobs.ordinary] == [300, 3600]
    assert [(job.name, job.interval) for job in jobs.prices] == [
        ("slow", price_interval),
    ]
    assert "cadence is best-effort" not in caplog.text


def test_per_feed_gap_capacity_warns_at_one_or_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    feeds = (
        make_feed("fast").model_copy(update={"ordinary_check_interval": 300}),
        make_feed("slow").model_copy(update={"ordinary_check_interval": 3600}),
    )

    class CaptureScheduler:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self) -> None:
            return

    monkeypatch.setattr("rss2discord.app.RuntimeScheduler", CaptureScheduler)
    caplog.set_level(logging.WARNING)
    with DeliveryStore(tmp_path / "state.db") as store:
        RSSToDiscord(
            AppConfig(feeds=feeds, delay_between_feeds=300),
            store,
            FakeSender([]),
        ).run()

    assert "cadence is best-effort" in caplog.text


@pytest.mark.parametrize("override", [False, True])
def test_tiny_ordinary_interval_runs_peer_after_nonzero_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    override: bool,
) -> None:
    tiny = make_feed("tiny").model_dump() | {
        "ordinary_check_interval": 1e-320 if override else None,
    }
    peer = make_feed("peer").model_dump() | {"ordinary_check_interval": 300}
    config = AppConfig.model_validate(
        {
            "feeds": [tiny, peer],
            "refresh_interval": 300 if override else 1e-320,
            "delay_between_feeds": 1,
        },
    )
    now = 0.0
    events: list[str] = []
    monkeypatch.setattr("rss2discord.app.time.monotonic", lambda: now)
    caplog.set_level(logging.WARNING)
    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(config, store, FakeSender([]))

        def process(feed: FeedConfig) -> None:
            nonlocal now
            events.append(feed.id)
            if feed.id == "tiny":
                now += 0.1
            else:
                app.request_shutdown()

        def sleep(seconds: float) -> bool:
            nonlocal now
            now += seconds
            return True

        monkeypatch.setattr(app, "_process_feed_safely", process)
        monkeypatch.setattr(app, "_interruptible_sleep", sleep)
        app.run()

    assert events == ["tiny", "peer"]
    assert "cadence is best-effort" in caplog.text


def test_cooldown_skip_does_not_delay_peer_or_change_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = make_feed("blocked")
    peer = make_feed("peer").model_copy(update={"url": "https://other.test/feed"})
    now = 0.0
    wall_now = int(time.time())
    sleeps: list[float] = []
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_health(
            HealthUpdate(
                blocked.id,
                "ordinary",
                "blocked",
                "BotChallenge",
                wall_now,
                False,
                False,
                0,
                0,
                0,
            ),
        )
        before = store.list_health(blocked.id)
        app = RSSToDiscord(
            AppConfig(feeds=(blocked, peer), delay_between_feeds=61),
            store,
            FakeSender([]),
        )
        app._strategies["rss"] = FakeStrategy([])
        monkeypatch.setattr("rss2discord.app.time.time", lambda: wall_now + 1)
        monkeypatch.setattr("rss2discord.app.time.monotonic", lambda: now)

        def sleep(seconds: float) -> bool:
            sleeps.append(seconds)
            app.request_shutdown()
            return False

        monkeypatch.setattr(app, "_interruptible_sleep", sleep)
        assert app._process_feed_safely(blocked) == JobOutcome.SKIPPED
        app.run()
        assert store.list_health(blocked.id) == before
        assert store.list_health(peer.id)[0].total_attempts == 1
        assert sleeps == [300]
