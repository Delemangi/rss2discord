import traceback
from time import struct_time

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


def test_rss_strategy_preserves_core_normalization_behavior() -> None:
    # Given
    strategy = RSSStrategy()
    entry = feedparser.FeedParserDict(
        {
            "id": " guid-42 ",
            "title": "Hello &amp; World",
            "link": "https://example.test/articles/42",
            "summary": "<p>Body &amp; <strong>more</strong></p>\nsubmitted by /u/source",
            "author": "Author",
            "published_parsed": struct_time((2026, 7, 20, 12, 34, 56, 0, 201, -1)),
        },
    )

    # When
    entry_id = strategy.get_entry_id(entry)
    entry_data = strategy.get_entry_data(entry)

    # Then
    assert entry_id == "guid-42"
    assert entry_data.title == "Hello & World"
    assert entry_data.link == "https://example.test/articles/42"
    assert entry_data.description == "Body & more"
    assert entry_data.timestamp == "2026-07-20T12:34:56+00:00"


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


def test_rss_strategy_parses_raw_timestamp_when_structured_time_is_missing() -> None:
    # Given
    strategy = RSSStrategy()
    entry = feedparser.FeedParserDict({"published": "2026-07-21T09:30:00+00:00"})

    # When / Then
    assert strategy._get_timestamp(entry) == "2026-07-21T09:30:00+00:00"


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

    def fail_request(
        url: str,
        *,
        headers: dict[str, str],
        timeout: int,
        stream: bool,
    ) -> requests.Response:
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

    class XenForoRequestError(Exception):
        pass

    class FailingScraper:
        def get_thread(self, url: str) -> None:
            raise XenForoRequestError(f"Could not connect to {url}")

    monkeypatch.setattr(xenforo_module, "xenforo", lambda **kwargs: FailingScraper())
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
