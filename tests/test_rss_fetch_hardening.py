from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import format_datetime
from types import TracebackType
from typing import Self

import pytest
import requests

from rss2discord.transports import FeedFetchError, RSSStrategy
from rss2discord.transports import rss as rss_transport

RSS_FEED_LIMIT_BYTES = 1_048_576
GITHUB_RELEASE_LIMIT_BYTES = 2_097_152
GITHUB_RELEASE_URL = "https://github.com/example/project/releases.atom"
VALID_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <link>https://example.test/</link>
    <description>Example</description>
    <item>
      <guid>entry-1</guid>
      <title>Entry One</title>
      <link>https://example.test/entry-1</link>
    </item>
  </channel>
</rss>
"""


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        chunks: list[bytes],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.url: object = None
        self.headers = headers or {}
        self.content = b"".join(chunks)
        self._chunks = chunks
        self.closed = False
        self.iter_content_calls = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        response = requests.Response()
        response.status_code = self.status_code
        response.headers.update(self.headers)
        raise requests.HTTPError(response=response)

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        self.iter_content_calls += 1
        yield from self._chunks


def install_response(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse,
    *,
    final_url: object = GITHUB_RELEASE_URL,
) -> None:
    response.url = final_url

    def get(url: str, **kwargs: int | bool | dict[str, str]) -> FakeResponse:
        del url, kwargs
        return response

    monkeypatch.setattr(requests, "get", get)


def sized_valid_feed(size: int) -> bytes:
    prefix = VALID_RSS[: -len(b"</rss>\n")]
    suffix = b"</rss>\n"
    comment_prefix = b"<!--"
    comment_suffix = b"-->"
    padding_size = (
        size - len(prefix) - len(suffix) - len(comment_prefix) - len(comment_suffix)
    )
    assert padding_size >= 0
    return prefix + comment_prefix + b"x" * padding_size + comment_suffix + suffix


def test_valid_rss_feed_streams_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = FakeResponse(200, [VALID_RSS[:80], VALID_RSS[80:]])
    install_response(monkeypatch, response)

    # When
    entries, title = RSSStrategy().fetch_entries("https://feed.test/rss")

    # Then
    assert title == "Example Feed"
    assert len(entries) == 1
    assert response.iter_content_calls == 1
    assert response.closed


@pytest.mark.parametrize(
    ("status_code", "retryable", "retry_after"),
    [
        (429, True, 12.5),
        (503, True, None),
        (404, False, None),
    ],
)
def test_http_error_exposes_only_safe_retry_metadata(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    retryable: bool,
    retry_after: float | None,
) -> None:
    # Given
    headers = {"Retry-After": "12.5"} if retry_after is not None else {}
    response = FakeResponse(status_code, [], headers)
    install_response(monkeypatch, response)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        RSSStrategy().fetch_entries("https://feed.test/rss?token=secret-token")

    # Then
    assert fetch_error.value.status_code == status_code
    assert fetch_error.value.retryable is retryable
    assert fetch_error.value.retry_after == retry_after
    assert "secret-token" not in str(fetch_error.value)
    assert response.iter_content_calls == 0
    assert response.closed


def test_http_date_retry_after_uses_shared_retry_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_at = datetime(2099, 7, 26, 15, 0, tzinfo=UTC)
    response = FakeResponse(
        429,
        [],
        {"Retry-After": format_datetime(retry_at, usegmt=True)},
    )
    install_response(monkeypatch, response)

    with pytest.raises(FeedFetchError) as fetch_error:
        RSSStrategy().fetch_entries("https://feed.test/rss")

    assert fetch_error.value.retryable
    assert fetch_error.value.retry_after is not None
    assert fetch_error.value.retry_after > 0


def test_content_length_over_limit_is_rejected_before_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = FakeResponse(
        200,
        [],
        {"Content-Length": str(RSS_FEED_LIMIT_BYTES + 1)},
    )
    install_response(monkeypatch, response)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        RSSStrategy().fetch_entries("https://feed.test/rss")

    # Then
    assert fetch_error.value.cause_type == "ResponseTooLarge"
    assert str(fetch_error.value) == "RSS fetch failed (ResponseTooLarge)"
    assert not fetch_error.value.retryable
    assert response.iter_content_calls == 0
    assert response.closed


def test_stream_over_limit_is_rejected_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = FakeResponse(200, [b"x" * RSS_FEED_LIMIT_BYTES, b"y"])
    install_response(monkeypatch, response)

    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        RSSStrategy().fetch_entries("https://feed.test/rss")

    # Then
    assert fetch_error.value.cause_type == "ResponseTooLarge"
    assert str(fetch_error.value) == "RSS fetch failed (ResponseTooLarge)"
    assert not fetch_error.value.retryable
    assert response.iter_content_calls == 1
    assert response.closed


def test_release_feed_accepts_large_valid_xml_with_existing_entry_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The padding is an XML comment, preserving the ordinary parsed entry.
    response = FakeResponse(200, [sized_valid_feed(1_166_061)])
    install_response(monkeypatch, response)

    entries, title = RSSStrategy().fetch_entries(GITHUB_RELEASE_URL)

    assert title == "Example Feed"
    assert len(entries) == 1
    assert RSSStrategy().get_entry_id(entries[0]) == "entry-1"


@pytest.mark.parametrize("declared", [False, True])
def test_release_feed_accepts_exact_limit_and_rejects_one_byte_over(
    monkeypatch: pytest.MonkeyPatch,
    declared: bool,
) -> None:
    exact = sized_valid_feed(GITHUB_RELEASE_LIMIT_BYTES)
    headers = {"Content-Length": str(len(exact))} if declared else {}
    response = FakeResponse(200, [exact], headers)
    install_response(monkeypatch, response)
    entries, _ = RSSStrategy().fetch_entries(GITHUB_RELEASE_URL)
    assert len(entries) == 1

    oversized = FakeResponse(
        200,
        [sized_valid_feed(GITHUB_RELEASE_LIMIT_BYTES + 1)],
        {"Content-Length": str(GITHUB_RELEASE_LIMIT_BYTES + 1)} if declared else {},
    )
    install_response(monkeypatch, oversized)
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        RSSStrategy().fetch_entries(GITHUB_RELEASE_URL)
    assert oversized.closed


def test_release_feed_rejects_declared_oversize_before_stream_or_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(
        200,
        [],
        {"Content-Length": str(GITHUB_RELEASE_LIMIT_BYTES + 1)},
    )
    install_response(monkeypatch, response)
    parse_called = False

    def parse(content: bytes) -> object:
        nonlocal parse_called
        parse_called = True
        return content

    monkeypatch.setattr(rss_transport.feedparser, "parse", parse)
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        RSSStrategy().fetch_entries(GITHUB_RELEASE_URL)
    assert response.iter_content_calls == 0
    assert not parse_called


def test_release_feed_caps_decoded_stream_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(
        200,
        [b"x" * GITHUB_RELEASE_LIMIT_BYTES, b"y"],
        {"Content-Encoding": "gzip"},
    )
    install_response(monkeypatch, response)
    parse_called = False

    def parse(content: bytes) -> object:
        nonlocal parse_called
        parse_called = True
        return content

    monkeypatch.setattr(rss_transport.feedparser, "parse", parse)
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        RSSStrategy().fetch_entries(GITHUB_RELEASE_URL)
    assert response.iter_content_calls == 1
    assert not parse_called


@pytest.mark.parametrize(
    "final_url",
    [
        "https://evil.example/releases.atom",
        "https://github.com/example/project/",
        None,
        42,
    ],
)
def test_release_request_gets_default_limit_unless_final_url_qualifies(
    monkeypatch: pytest.MonkeyPatch,
    final_url: object,
) -> None:
    response = FakeResponse(200, [b"x" * (RSS_FEED_LIMIT_BYTES + 1)])
    install_response(monkeypatch, response, final_url=final_url)
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        RSSStrategy().fetch_entries(GITHUB_RELEASE_URL)


def test_nonrelease_request_gets_default_limit_even_if_final_url_qualifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(200, [b"x" * (RSS_FEED_LIMIT_BYTES + 1)])
    install_response(monkeypatch, response, final_url=GITHUB_RELEASE_URL)
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        RSSStrategy().fetch_entries("https://example.test/feed.xml")


def test_shared_strategy_does_not_retain_release_budget_between_feeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = RSSStrategy()
    large_release = FakeResponse(200, [sized_valid_feed(RSS_FEED_LIMIT_BYTES + 1)])
    ordinary = FakeResponse(200, [b"x" * (RSS_FEED_LIMIT_BYTES + 1)])
    large_release.url = GITHUB_RELEASE_URL
    ordinary.url = "https://example.test/feed.xml"
    responses = iter([large_release, ordinary])

    def get(url: str, **kwargs: int | bool | dict[str, str]) -> FakeResponse:
        del url, kwargs
        return next(responses)

    monkeypatch.setattr(requests, "get", get)
    strategy.fetch_entries(GITHUB_RELEASE_URL)
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        strategy.fetch_entries("https://example.test/feed.xml")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b/releases.atom",
        "https://www.github.com/a/b/releases.atom",
        "https://github.com.evil.test/a/b/releases.atom",
        "https://user@github.com/a/b/releases.atom",
        "https://github.com:443/a/b/releases.atom",
        "https://github.com/a/b/releases.atom?token=x",
        "https://github.com/a/b/releases.atom#fragment",
        "https://github.com/a/b/releases.atom/extra",
        "https://github.com/a%2fb/b/releases.atom",
        "https://github.com/../b/releases.atom",
        "https://github.com:invalid/a/b/releases.atom",
    ],
)
def test_release_atom_url_classifier_rejects_lookalikes(url: str) -> None:
    assert not rss_transport._is_github_release_atom_url(url)


def test_release_atom_url_classifier_accepts_strict_public_address() -> None:
    assert rss_transport._is_github_release_atom_url(GITHUB_RELEASE_URL)
