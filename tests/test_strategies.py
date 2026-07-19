import traceback

import feedparser
import pytest
import requests

import strategies.xenforo_strategy as xenforo_module
from strategies import FeedFetchError, RSSStrategy, XenForoStrategy


def test_rss_strategy_uses_stable_native_identity() -> None:
    # Given
    strategy = RSSStrategy()
    identified_entry = feedparser.FeedParserDict({"id": "guid-1"})
    linked_entry = feedparser.FeedParserDict({"link": "https://example.test/1"})
    unidentified_entry = feedparser.FeedParserDict({"title": "No stable identity"})

    # When / Then
    assert strategy.get_entry_id(identified_entry) == "guid-1"
    assert strategy.get_entry_id(linked_entry) == "https://example.test/1"
    assert strategy.get_entry_id(unidentified_entry) is None


def test_xenforo_strategy_requires_post_id() -> None:
    # Given
    strategy = XenForoStrategy()

    # When / Then
    assert strategy.get_entry_id({"id": 42}) == "42"
    assert strategy.get_entry_id({"content": "No stable identity"}) is None


def test_rss_strategy_does_not_invent_missing_timestamp() -> None:
    # Given
    strategy = RSSStrategy()
    entry = feedparser.FeedParserDict({"published": "not-a-date"})

    # When / Then
    assert strategy._get_timestamp(entry) is None


def test_xenforo_strategy_does_not_invent_missing_timestamp() -> None:
    # Given
    strategy = XenForoStrategy()

    # When / Then
    assert strategy._get_timestamp({}) is None


def test_rss_fetch_error_does_not_expose_feed_url_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed_url = "https://feed.test/rss?token=secret-token"

    def fail_request(url: str, **kwargs: object) -> requests.Response:
        del kwargs
        raise requests.ConnectionError(f"Could not connect to {url}")

    monkeypatch.setattr(requests, "get", fail_request)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        RSSStrategy().fetch_entries(feed_url)

    # Then
    rendered_error = "".join(
        traceback.format_exception(
            fetch_error.type,
            fetch_error.value,
            fetch_error.tb,
        ),
    )
    assert "secret-token" not in rendered_error


def test_xenforo_fetch_error_does_not_expose_feed_url_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed_url = "https://feed.test/thread?token=secret-token"

    class FailingScraper:
        def get_thread(self, url: str) -> None:
            raise RuntimeError(f"Could not connect to {url}")

    monkeypatch.setattr(
        xenforo_module,
        "xenforo",
        lambda **kwargs: FailingScraper(),
    )

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        XenForoStrategy().fetch_entries(feed_url)

    # Then
    rendered_error = "".join(
        traceback.format_exception(
            fetch_error.type,
            fetch_error.value,
            fetch_error.tb,
        ),
    )
    assert "secret-token" not in rendered_error
