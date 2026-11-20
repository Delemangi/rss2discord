"""Validate the configured Reddit listing URL scope parser."""

import pytest

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.reddit.scope import RedditScope, parse_reddit_scope

ACCEPTED_SCOPES = [
    ("https://www.reddit.com/r/python", RedditScope("python", "new", None)),
    ("https://www.reddit.com/r/python/", RedditScope("python", "new", None)),
    ("https://old.reddit.com/r/python/new", RedditScope("python", "new", None)),
    ("https://www.reddit.com/r/python/hot", RedditScope("python", "hot", None)),
    (
        "https://www.reddit.com/r/python/rising",
        RedditScope("python", "rising", None),
    ),
    (
        "https://www.reddit.com/r/python/top",
        RedditScope("python", "top", None),
    ),
    (
        "https://www.reddit.com/r/python/top?t=hour",
        RedditScope("python", "top", "hour"),
    ),
    (
        "https://old.reddit.com/r/python/top?t=day",
        RedditScope("python", "top", "day"),
    ),
    (
        "https://www.reddit.com/r/python/top?t=week",
        RedditScope("python", "top", "week"),
    ),
    (
        "https://www.reddit.com/r/python/top?t=month",
        RedditScope("python", "top", "month"),
    ),
    (
        "https://www.reddit.com/r/python/top?t=year",
        RedditScope("python", "top", "year"),
    ),
    (
        "https://www.reddit.com/r/python/top?t=all",
        RedditScope("python", "top", "all"),
    ),
    (
        "https://www.reddit.com/r/python/controversial?t=all",
        RedditScope("python", "controversial", "all"),
    ),
    (
        "https://www.reddit.com/r/Python_2/TOP?t=day",
        RedditScope("Python_2", "top", "day"),
    ),
]

REJECTED_URLS = [
    "http://www.reddit.com/r/python/new",
    "https://reddit.com/r/python/new",
    "https://api.reddit.com/r/python/new",
    "https://evil.test/r/python/new",
    "https://www.reddit.com/r/python+rust/new",
    "https://www.reddit.com/user/someone/new",
    "https://www.reddit.com/comments/abc123/new",
    "https://user:password@www.reddit.com/r/python/new",
    "https://www.reddit.com:8443/r/python/new",
    "https://www.reddit.com/r/python/new#fragment",
    "https://www.reddit.com/r/python/best",
    "https://www.reddit.com/r/",
    "https://www.reddit.com/r/python/hot?t=day",
    "https://www.reddit.com/r/python/new?t=all",
    "https://www.reddit.com/r/python/top?t=decade",
    "https://www.reddit.com/r/python/top?t=",
    "https://www.reddit.com/r/python/top?t=day&t=week",
    "https://www.reddit.com/r/python/top?limit=100",
    "https://www.reddit.com/r/python//new",
    "https://www.reddit.com/r/abcdefghijklmnopqrstuvwxyz/new",
    "https://www.reddit.com/r/some-subreddit/new",
    "https://www.reddit.com/r/python/new/extra",
    "https://www.reddit.com/",
]


@pytest.mark.parametrize(("url", "expected"), ACCEPTED_SCOPES)
def test_accepts_supported_listing_urls(url: str, expected: RedditScope) -> None:
    assert parse_reddit_scope(url) == expected


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_rejects_unsupported_listing_urls(url: str) -> None:
    with pytest.raises(FeedFetchError) as error:
        parse_reddit_scope(url)

    assert error.value.strategy == "Reddit"
    assert error.value.cause_type == "InvalidUrl"
    assert error.value.retryable is False
