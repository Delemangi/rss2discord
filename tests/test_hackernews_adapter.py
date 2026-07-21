import pytest
import requests

import adapters.hackernews as hackernews_module
from adapters.hackernews import (
    HACKER_NEWS_TIMEOUT_SECONDS,
    HackerNewsAdapter,
    HackerNewsItem,
    fetch_hacker_news_item,
)
from models import EntryData, SourceMetric


def make_entry(
    *,
    link: str = "https://example.test/article",
    discussion_url: str | None = "https://news.ycombinator.com/item?id=123",
) -> EntryData:
    return EntryData(
        title="Example story",
        link=link,
        description="Comments",
        author="",
        timestamp=None,
        discussion_url=discussion_url,
    )


def test_hackernews_adapter_enriches_external_story() -> None:
    # Given
    item = HackerNewsItem(
        id=123,
        type="story",
        by="alice",
        time=1_752_921_600,
        url="https://www.example.test/article",
        score=111,
        descendants=71,
    )
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: item)

    # When
    result = adapter.adapt({}, make_entry())

    # Then
    assert result.author == "alice"
    assert result.description == ""
    assert result.link == "https://www.example.test/article"
    assert result.timestamp == "2025-07-19T10:40:00+00:00"
    assert result.source_metrics == (
        SourceMetric(label="Points", value="111"),
        SourceMetric(label="Comments", value="71"),
        SourceMetric(label="Domain", value="example.test"),
    )


def test_hackernews_adapter_uses_self_post_text() -> None:
    # Given
    item = HackerNewsItem(
        id=123,
        type="story",
        by="bob",
        text="Hello <b>HN</b><p>Second paragraph",
        score=25,
        descendants=16,
    )
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: item)
    entry = make_entry(
        link="https://news.ycombinator.com/item?id=123",
    )

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result.description == "Hello HN\nSecond paragraph"
    assert result.link == entry.link
    assert result.source_metrics == (
        SourceMetric(label="Points", value="25"),
        SourceMetric(label="Comments", value="16"),
    )


def test_hackernews_adapter_skips_entry_without_item_id() -> None:
    # Given
    fetched_ids: list[int] = []
    adapter = HackerNewsAdapter(fetch_item=fetched_ids.append)
    entry = make_entry(discussion_url=None)

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result is entry
    assert fetched_ids == []


def test_hackernews_adapter_preserves_entry_when_api_item_is_unavailable() -> None:
    # Given
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: None)
    entry = make_entry()

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result is entry


def test_hackernews_adapter_preserves_deleted_item() -> None:
    # Given
    item = HackerNewsItem(id=123, type="story", deleted=True)
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: item)
    entry = make_entry()

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result is entry


def test_hackernews_adapter_preserves_timestamp_when_api_time_is_out_of_range() -> None:
    # Given
    item = HackerNewsItem(id=123, type="story", time=10**100)
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: item)
    entry = make_entry()
    entry = EntryData(
        title=entry.title,
        link=entry.link,
        description=entry.description,
        author=entry.author,
        timestamp="2026-07-21T09:00:00+00:00",
        discussion_url=entry.discussion_url,
    )

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result.timestamp == entry.timestamp


def test_hackernews_adapter_preserves_rss_link_for_blank_api_url() -> None:
    # Given
    item = HackerNewsItem(id=123, type="story", url="   ")
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: item)
    entry = make_entry()

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result.link == entry.link


def test_fetch_hacker_news_item_parses_api_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, int | str]:
            return {"id": 123, "type": "story", "by": "alice", "score": 42}

    def fake_get(url: str, *, timeout: int) -> FakeResponse:
        assert url.endswith("/123.json")
        assert timeout == HACKER_NEWS_TIMEOUT_SECONDS
        return FakeResponse()

    monkeypatch.setattr(hackernews_module.requests, "get", fake_get)

    # When
    item = fetch_hacker_news_item(123)

    # Then
    assert item == HackerNewsItem(id=123, type="story", by="alice", score=42)


def test_fetch_hacker_news_item_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def fail_get(url: str, *, timeout: int) -> requests.Response:
        del url, timeout
        raise requests.Timeout

    monkeypatch.setattr(hackernews_module.requests, "get", fail_get)

    # When / Then
    assert fetch_hacker_news_item(123) is None
