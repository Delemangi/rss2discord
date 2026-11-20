"""Immutable request, response, and pagination limits for Reddit feeds."""

from typing import Final

REDDIT_LABEL: Final = "Reddit"
REDDIT_TOKEN_URL: Final = "https://www.reddit.com/api/v1/access_token"  # noqa: S105
REDDIT_API_ORIGIN: Final = "https://oauth.reddit.com"
REDDIT_DISCUSSION_ORIGIN: Final = "https://www.reddit.com"
REDDIT_HOSTS: Final = frozenset({"www.reddit.com", "old.reddit.com"})
REDDIT_SORTS: Final = frozenset(
    {"new", "hot", "top", "rising", "controversial"},
)
REDDIT_TIME_FILTERS: Final = frozenset(
    {"hour", "day", "week", "month", "year", "all"},
)
REDDIT_TIME_SORTS: Final = frozenset({"top", "controversial"})
REDDIT_DEFAULT_SORT: Final = "new"
REDDIT_PAGE_SIZE: Final = 100
MAX_REDDIT_PAGES: Final = 3
MAX_REDDIT_POSTS: Final = REDDIT_PAGE_SIZE * MAX_REDDIT_PAGES
MAX_REDDIT_RESPONSE_BYTES: Final = 8 * 1024 * 1024
REDDIT_STREAM_CHUNK_BYTES: Final = 65_536
REDDIT_REQUEST_SECONDS: Final = 30.0
REDDIT_MIN_REQUEST_INTERVAL_SECONDS: Final = 0.6
REDDIT_TOKEN_SAFETY_SECONDS: Final = 60
REDDIT_DEFAULT_TOKEN_SECONDS: Final = 3_600
REDDIT_DEFAULT_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
REDDIT_CLIENT_ID_ENV: Final = "REDDIT_CLIENT_ID"
REDDIT_CLIENT_SECRET_ENV: Final = "REDDIT_CLIENT_SECRET"  # noqa: S105
REDDIT_USER_AGENT_ENV: Final = "REDDIT_USER_AGENT"
REDDIT_ACCESS_TOKEN_ENV: Final = "REDDIT_ACCESS_TOKEN"  # noqa: S105
