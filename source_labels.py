"""Discord source labels for configured feed transports and adapters."""

from typing import Final, assert_never
from urllib.parse import urlsplit

from configuration import FeedConfig

SOURCE_LABEL_FORUM: Final = "Forum"
SOURCE_LABEL_REDDIT: Final = "Reddit"
SOURCE_LABEL_HACKER_NEWS: Final = "Hacker News"
SOURCE_LABEL_RSS: Final = "RSS"


def source_label(feed: FeedConfig) -> str:
    match feed.adapter:
        case "hackernews":
            return SOURCE_LABEL_HACKER_NEWS
        case "reddit":
            return SOURCE_LABEL_REDDIT
        case None:
            pass
        case unreachable:
            assert_never(unreachable)
    if feed.strategy == "xenforo":
        return SOURCE_LABEL_FORUM
    try:
        hostname = urlsplit(feed.url).hostname
    except ValueError:
        return SOURCE_LABEL_RSS
    if hostname is None:
        return SOURCE_LABEL_RSS
    hostname_lower = hostname.lower()
    if hostname_lower == "news.ycombinator.com":
        return SOURCE_LABEL_HACKER_NEWS
    if hostname_lower == "reddit.com" or hostname_lower.endswith(".reddit.com"):
        return SOURCE_LABEL_REDDIT
    return SOURCE_LABEL_RSS
