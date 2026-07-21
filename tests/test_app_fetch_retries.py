import logging
from pathlib import Path
from typing import Any, assert_never

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import SleepCallback, WebhookMessage
from rss2discord.models import EntryData, EntryId
from rss2discord.transports import FeedFetchError, ScraperStrategy

type FetchResult = tuple[list[Any], str]


class RetryStrategy(ScraperStrategy):
    def __init__(self, outcomes: list[FetchResult | FeedFetchError]) -> None:
        self._outcomes = outcomes
        self.attempts = 0

    def fetch_entries(self, url: str) -> FetchResult:
        del url
        self.attempts += 1
        outcome = self._outcomes.pop(0)
        match outcome:
            case FeedFetchError():
                raise outcome
            case tuple():
                return outcome
            case _ as unreachable:
                assert_never(unreachable)

    def get_entry_id(self, entry: Any) -> EntryId | None:  # noqa: ANN401
        del entry
        return None

    def get_entry_data(self, entry: Any) -> EntryData:  # noqa: ANN401
        raise AssertionError(entry)


class UnusedSender:
    def send(self, message: WebhookMessage, sleep: SleepCallback) -> bool:
        del message, sleep
        raise AssertionError("sender should not be called")


def make_feed() -> FeedConfig:
    return FeedConfig(
        id="news",
        name="News",
        url="https://feed.test/rss?token=secret-token",
        webhook="https://discord.test/webhook",
    )


def make_app(
    store: DeliveryStore,
    strategy: RetryStrategy,
    feed: FeedConfig,
) -> RSSToDiscord:
    app = RSSToDiscord(
        config=AppConfig(feeds=(feed,)),
        store=store,
        sender=UnusedSender(),
    )
    app._strategies["rss"] = strategy
    return app


def record_sleep(delays: list[float], seconds: float) -> bool:
    delays.append(seconds)
    return True


def test_retry_after_is_capped_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = make_feed()
    strategy = RetryStrategy(
        [
            FeedFetchError(
                "RSS",
                "HTTPError",
                status_code=429,
                retryable=True,
                retry_after=9999,
            ),
            ([], "News"),
        ],
    )
    delays: list[float] = []

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, strategy, feed)
        monkeypatch.setattr(
            app,
            "_interruptible_sleep",
            lambda seconds: record_sleep(delays, seconds),
        )

        # When
        app.process_feed(feed)

    # Then
    assert strategy.attempts == 2
    assert delays == [300.0]


def test_missing_retry_after_uses_jittered_exponential_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = make_feed()
    strategy = RetryStrategy(
        [
            FeedFetchError("RSS", "Timeout", retryable=True),
            FeedFetchError("RSS", "ConnectionError", retryable=True),
            ([], "News"),
        ],
    )
    delays: list[float] = []

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, strategy, feed)
        monkeypatch.setattr(
            app,
            "_interruptible_sleep",
            lambda seconds: record_sleep(delays, seconds),
        )

        # When
        app.process_feed(feed)

    # Then
    assert strategy.attempts == 3
    assert len(delays) == 2
    assert 0 <= delays[0] <= 2.0
    assert 0 <= delays[1] <= 4.0


def test_retryable_fetch_failure_is_exhausted_after_three_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    feed = make_feed()
    strategy = RetryStrategy(
        [
            FeedFetchError("RSS", "HTTPError", status_code=503, retryable=True),
            FeedFetchError("RSS", "HTTPError", status_code=503, retryable=True),
            FeedFetchError("RSS", "HTTPError", status_code=503, retryable=True),
        ],
    )
    delays: list[float] = []
    caplog.set_level(logging.ERROR)

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, strategy, feed)
        monkeypatch.setattr(
            app,
            "_interruptible_sleep",
            lambda seconds: record_sleep(delays, seconds),
        )

        # When
        app._process_feed_safely(feed)

    # Then
    assert strategy.attempts == 3
    assert len(delays) == 2
    assert "HTTP 503" in caplog.text
    assert "secret-token" not in caplog.text


def test_permanent_fetch_failure_is_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed = make_feed()
    strategy = RetryStrategy(
        [FeedFetchError("RSS", "HTTPError", status_code=404)],
    )
    delays: list[float] = []

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, strategy, feed)
        monkeypatch.setattr(
            app,
            "_interruptible_sleep",
            lambda seconds: record_sleep(delays, seconds),
        )

        # When
        app._process_feed_safely(feed)

    # Then
    assert strategy.attempts == 1
    assert delays == []


def test_shutdown_during_fetch_retry_is_not_logged_as_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    feed = make_feed()
    strategy = RetryStrategy(
        [FeedFetchError("RSS", "Timeout", retryable=True)],
    )
    caplog.set_level(logging.ERROR)

    with DeliveryStore(tmp_path / "state.db") as store:
        app = make_app(store, strategy, feed)

        def request_shutdown(_seconds: float) -> bool:
            app.request_shutdown()
            return False

        monkeypatch.setattr(app, "_interruptible_sleep", request_shutdown)

        # When
        app._process_feed_safely(feed)

    # Then
    assert strategy.attempts == 1
    assert app._shutdown_requested
    assert not caplog.records
