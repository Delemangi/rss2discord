from collections.abc import Iterator
from types import TracebackType
from typing import Self

import pytest
import requests

from strategies import FeedFetchError, RSSStrategy

RSS_FEED_LIMIT_BYTES = 1_048_576
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
) -> None:
    def get(url: str, **kwargs: int | bool | dict[str, str]) -> FakeResponse:
        del url, kwargs
        return response

    monkeypatch.setattr(requests, "get", get)


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
