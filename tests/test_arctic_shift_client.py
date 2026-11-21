"""Arctic Shift client query, parsing, and failure evidence."""

from pathlib import Path
from typing import Any

import pytest

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.arctic_shift.bounds import ARCTIC_SHIFT_USER_AGENT
from rss2discord.providers.arctic_shift.client import ArcticShiftClient
from rss2discord.providers.reddit.scope import RedditScope
from rss2discord.retries import FeedFetchInterruptedError
from tests.reddit_helpers import FakeResponse, FakeSession

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "arctic_shift"
SCOPE = RedditScope(subreddit="python", sort="new")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def make_client(
    session: FakeSession,
    **kwargs: Any,  # noqa: ANN401
) -> ArcticShiftClient:
    return ArcticShiftClient(session=session, **kwargs)


def test_fetch_listing_builds_the_newest_query() -> None:
    # Given
    session = FakeSession([FakeResponse(200, fixture_bytes("posts-page.json"))])
    client = make_client(session)

    # When
    page = client.fetch_listing(SCOPE)

    # Then
    assert [post.name for post in page.posts] == [
        "t3_stickied",
        "t3_nsfw",
        "t3_deleted_author",
        "t3_removed2",
        "t3_removed1",
        "t3_gallery1",
        "t3_image1",
        "t3_self1",
        "t3_link1",
    ]
    assert page.after is None
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://arctic-shift.photon-reddit.com/api/posts/search"
    assert kwargs["params"] == {"subreddit": "python", "limit": 100, "sort": "desc"}
    assert kwargs["headers"] == {"User-Agent": ARCTIC_SHIFT_USER_AGENT}
    assert kwargs["stream"] is True


def test_fetch_listing_sends_the_before_cursor() -> None:
    # Given
    session = FakeSession([FakeResponse(200, fixture_bytes("posts-page.json"))])
    client = make_client(session)

    # When
    client.fetch_listing(SCOPE, after="1700000000")

    # Then
    assert session.calls[0][2]["params"] == {
        "subreddit": "python",
        "limit": 100,
        "sort": "desc",
        "before": "1700000000",
    }


def test_fetch_listing_skips_malformed_posts_and_advances_the_cursor() -> None:
    # Given
    session = FakeSession(
        [FakeResponse(200, fixture_bytes("posts-page-malformed.json"))],
    )
    client = make_client(session, page_size=3)

    # When
    page = client.fetch_listing(SCOPE)

    # Then
    assert [post.name for post in page.posts] == ["t3_valid1", "t3_valid2"]
    assert page.after == "1700000100"


def test_fetch_listing_rejects_non_new_sorts_without_a_request() -> None:
    # Given
    session = FakeSession([])
    client = make_client(session)

    # When / Then
    with pytest.raises(FeedFetchError, match="UnsupportedSort"):
        client.fetch_listing(RedditScope(subreddit="python", sort="hot"))
    assert session.calls == []


def test_fetch_listing_stops_before_a_request_when_shutdown() -> None:
    # Given
    session = FakeSession([])
    client = make_client(session, is_shutdown_requested=lambda: True)

    # When / Then
    with pytest.raises(FeedFetchInterruptedError):
        client.fetch_listing(SCOPE)
    assert session.calls == []


def test_fetch_listing_maps_429_to_a_retryable_rate_limit() -> None:
    # Given
    session = FakeSession([FakeResponse(429, b"", {"Retry-After": "12"})])
    client = make_client(session)

    # When / Then
    with pytest.raises(FeedFetchError) as info:
        client.fetch_listing(SCOPE)
    assert info.value.status_code == 429
    assert info.value.retryable is True
    assert info.value.retry_after == 12.0


def test_fetch_listing_parses_a_rate_limit_delta_but_ignores_an_epoch() -> None:
    # Given
    delta_session = FakeSession(
        [FakeResponse(429, b"", {"X-RateLimit-Reset": "30"})],
    )
    epoch_session = FakeSession(
        [FakeResponse(429, b"", {"X-RateLimit-Reset": "1795270531"})],
    )

    # When / Then
    with pytest.raises(FeedFetchError) as delta:
        make_client(delta_session).fetch_listing(SCOPE)
    with pytest.raises(FeedFetchError) as epoch:
        make_client(epoch_session).fetch_listing(SCOPE)
    assert delta.value.retry_after == 30.0
    assert epoch.value.retry_after is None


def test_fetch_listing_maps_5xx_to_retryable_and_4xx_to_permanent() -> None:
    # Given
    server_session = FakeSession([FakeResponse(503)])
    client_session = FakeSession([FakeResponse(404)])

    # When / Then
    with pytest.raises(FeedFetchError) as server:
        make_client(server_session).fetch_listing(SCOPE)
    with pytest.raises(FeedFetchError) as client:
        make_client(client_session).fetch_listing(SCOPE)
    assert server.value.retryable is True
    assert client.value.retryable is False


def test_fetch_listing_rejects_invalid_json() -> None:
    # Given
    session = FakeSession([FakeResponse(200, b"not-json")])
    client = make_client(session)

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidJson"):
        client.fetch_listing(SCOPE)


def test_fetch_listing_enforces_the_response_byte_cap() -> None:
    # Given
    session = FakeSession([FakeResponse(200, b"x" * 100)])
    client = make_client(session, max_response_bytes=10)

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        client.fetch_listing(SCOPE)
