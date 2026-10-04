import logging
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.recovery_models import HealthUpdate
from rss2discord.scheduler import JobOutcome
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
