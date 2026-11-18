from urllib.parse import quote

import pytest
import requests

import rss2discord.adapters.hackernews as hackernews_module
from rss2discord.adapters.hackernews import (
    HACKER_NEWS_TIMEOUT_SECONDS,
    HackerNewsAdapter,
    HackerNewsItem,
    fetch_hacker_news_item,
    fetch_hacker_news_topstories,
)
from rss2discord.models import EntryData, SourceMetric
from tests.discord_components_helpers import get_text_display_contents, make_message


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


def test_hackernews_adapter_adds_front_page_rank() -> None:
    # Given
    item = HackerNewsItem(
        id=123,
        type="story",
        by="alice",
        url="https://example.test/article",
        score=111,
        descendants=71,
    )
    adapter = HackerNewsAdapter(
        fetch_item=lambda item_id: item,
        fetch_topstories=lambda: (3, 1, 123),
    )

    # When
    result = adapter.adapt({}, make_entry())

    # Then
    assert result.source_metrics == (
        SourceMetric(label="Rank", value="#3"),
        SourceMetric(label="Points", value="111"),
        SourceMetric(label="Comments", value="71"),
        SourceMetric(label="Domain", value="example.test"),
    )


def test_hackernews_adapter_uses_top_comment_for_link_post() -> None:
    # Given
    story = HackerNewsItem(
        id=123,
        type="story",
        by="alice",
        url="https://example.test/article",
        score=10,
        descendants=2,
        kids=(999,),
    )
    comment = HackerNewsItem(
        id=999,
        type="comment",
        by="carol",
        text="Insightful <i>comment</i>",
    )

    def fetch(item_id: int) -> HackerNewsItem:
        return comment if item_id == 999 else story

    adapter = HackerNewsAdapter(fetch_item=fetch)

    # When
    result = adapter.adapt({}, make_entry())

    # Then
    assert result.description == "Top comment: Insightful comment — carol"
    assert result.author == "alice"


def test_hackernews_adapter_prefers_self_post_text_over_top_comment() -> None:
    # Given
    story = HackerNewsItem(
        id=123,
        type="story",
        by="bob",
        text="Self text",
        kids=(999,),
    )
    fetched: list[int] = []

    def fetch(item_id: int) -> HackerNewsItem:
        fetched.append(item_id)
        return story

    adapter = HackerNewsAdapter(fetch_item=fetch)
    entry = make_entry(link="https://news.ycombinator.com/item?id=123")

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result.description == "Self text"
    assert fetched == [123]


def test_hackernews_adapter_skips_deleted_top_comment() -> None:
    # Given
    story = HackerNewsItem(
        id=123,
        type="story",
        url="https://example.test/article",
        kids=(999,),
    )
    comment = HackerNewsItem(id=999, type="comment", deleted=True)
    adapter = HackerNewsAdapter(
        fetch_item=lambda item_id: comment if item_id == 999 else story,
    )

    # When
    result = adapter.adapt({}, make_entry())

    # Then
    assert result.description == ""


def test_hackernews_adapter_adds_type_for_job() -> None:
    # Given
    item = HackerNewsItem(
        id=123,
        type="job",
        by="alice",
        url="https://example.test/job",
        score=1,
    )
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: item)

    # When
    result = adapter.adapt({}, make_entry())

    # Then
    assert result.source_metrics == (
        SourceMetric(label="Points", value="1"),
        SourceMetric(label="Domain", value="example.test"),
        SourceMetric(label="Type", value="Job"),
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


@pytest.mark.parametrize("raw_item_id", ["²", "9" * 5000])
def test_hackernews_adapter_ignores_nonconvertible_item_id(raw_item_id: str) -> None:
    # Given
    fetched_ids: list[int] = []
    adapter = HackerNewsAdapter(fetch_item=fetched_ids.append)
    entry = make_entry(
        discussion_url=("https://news.ycombinator.com/item?id=" + quote(raw_item_id)),
    )

    # When
    result = adapter.adapt({}, entry)

    # Then
    assert result is entry
    assert fetched_ids == []


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


@pytest.mark.parametrize(
    "api_url",
    ["javascript:alert(1)", "/relative/article", "https://example.test/bad\nlink"],
)
def test_hackernews_adapter_preserves_rss_link_for_invalid_api_url(
    api_url: str,
) -> None:
    # Given
    item = HackerNewsItem(id=123, type="story", url=api_url)
    adapter = HackerNewsAdapter(fetch_item=lambda item_id: item)
    entry = make_entry()

    # When
    result = adapter.adapt({}, entry)
    heading = get_text_display_contents(
        make_message(adapter="hackernews", entry=result),
    )[0]

    # Then
    assert result.link == entry.link
    assert result.source_metrics == (
        SourceMetric(label="Domain", value="example.test"),
    )
    assert entry.link in heading


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


def test_fetch_hacker_news_topstories_parses_api_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[int]:
            return [1, 2, 3]

    def fake_get(url: str, *, timeout: int) -> FakeResponse:
        assert url.endswith("/topstories.json")
        assert timeout == HACKER_NEWS_TIMEOUT_SECONDS
        return FakeResponse()

    monkeypatch.setattr(hackernews_module.requests, "get", fake_get)

    # When / Then
    assert fetch_hacker_news_topstories() == (1, 2, 3)


def test_fetch_hacker_news_topstories_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def fail_get(url: str, *, timeout: int) -> requests.Response:
        del url, timeout
        raise requests.Timeout

    monkeypatch.setattr(hackernews_module.requests, "get", fail_get)

    # When / Then
    assert fetch_hacker_news_topstories() is None


def test_fetch_hacker_news_topstories_rejects_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[object]:
            return [1, "two"]

    def fake_get(url: str, *, timeout: int) -> FakeResponse:
        del url, timeout
        return FakeResponse()

    monkeypatch.setattr(hackernews_module.requests, "get", fake_get)

    # When / Then
    assert fetch_hacker_news_topstories() is None
