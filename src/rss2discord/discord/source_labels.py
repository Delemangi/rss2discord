from typing import Final, assert_never
from urllib.parse import urlsplit

from rss2discord.configuration import FeedConfig

SOURCE_LABEL_FORUM: Final = "Forum"
SOURCE_LABEL_GITHUB: Final = "GitHub"
SOURCE_LABEL_ITMK_OGLASNIK: Final = "IT.mk Oglasnik"
SOURCE_LABEL_REDDIT: Final = "Reddit"
SOURCE_LABEL_HACKER_NEWS: Final = "Hacker News"
SOURCE_LABEL_RSS: Final = "RSS"


def source_label(feed: FeedConfig) -> str:
    if feed.adapter is not None:
        match feed.adapter:
            case "hackernews":
                return SOURCE_LABEL_HACKER_NEWS
            case "reddit":
                return SOURCE_LABEL_REDDIT
            case unreachable:
                assert_never(unreachable)
    match feed.strategy:
        case "xenforo":
            return SOURCE_LABEL_FORUM
        case "itmk_oglasnik":
            return SOURCE_LABEL_ITMK_OGLASNIK
        case "rss":
            return _rss_source_label(feed.url)
        case unreachable_strategy:
            assert_never(unreachable_strategy)


def _rss_source_label(url: str) -> str:
    try:
        parsed_url = urlsplit(url)
        hostname = parsed_url.hostname
    except ValueError:
        return SOURCE_LABEL_RSS
    if hostname is None:
        return SOURCE_LABEL_RSS
    hostname_lower = hostname.lower()
    path_segments = tuple(segment for segment in parsed_url.path.split("/") if segment)
    if (
        hostname_lower == "github.com"
        and len(path_segments) == 3
        and path_segments[-1] == "releases.atom"
    ):
        return SOURCE_LABEL_GITHUB
    if hostname_lower == "news.ycombinator.com":
        return SOURCE_LABEL_HACKER_NEWS
    if hostname_lower == "reddit.com" or hostname_lower.endswith(".reddit.com"):
        return SOURCE_LABEL_REDDIT
    return SOURCE_LABEL_RSS
