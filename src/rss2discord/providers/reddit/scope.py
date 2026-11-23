"""Parse a configured Reddit listing URL into an immutable query scope."""

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qs, urlsplit

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.reddit.bounds import (
    REDDIT_DEFAULT_SORT,
    REDDIT_HOSTS,
    REDDIT_LABEL,
    REDDIT_SORTS,
    REDDIT_TIME_FILTERS,
    REDDIT_TIME_SORTS,
)

SUBREDDIT_PATTERN: Final = re.compile(r"\w{1,21}", flags=re.ASCII)


@dataclass(frozen=True, slots=True)
class RedditScope:
    """One subreddit listing endpoint and its optional time window."""

    subreddit: str
    sort: str
    time_filter: str | None = None


def parse_reddit_scope(url: str) -> RedditScope:
    """Validate a public subreddit URL and return its listing scope."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or hostname is None
        or hostname.casefold() not in REDDIT_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl")
    segments = _path_segments(parsed.path)
    if len(segments) not in {2, 3} or segments[0] != "r":
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl")
    subreddit = segments[1]
    if SUBREDDIT_PATTERN.fullmatch(subreddit) is None:
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl")
    sort = segments[2].casefold() if len(segments) == 3 else REDDIT_DEFAULT_SORT
    if sort not in REDDIT_SORTS:
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl")
    return RedditScope(
        subreddit=subreddit,
        sort=sort,
        time_filter=_time_filter(parsed.query, sort),
    )


def _path_segments(path: str) -> list[str]:
    if not path.startswith("/"):
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl")
    segments = path[1:].split("/")
    if segments and segments[-1] == "":
        segments.pop()
    return segments


def _time_filter(query: str, sort: str) -> str | None:
    if not query:
        return None
    params = parse_qs(query, keep_blank_values=True)
    if set(params) != {"t"} or len(params["t"]) != 1:
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl")
    value = params["t"][0].strip().casefold()
    if sort not in REDDIT_TIME_SORTS or value not in REDDIT_TIME_FILTERS:
        raise FeedFetchError(REDDIT_LABEL, "InvalidUrl")
    return value
