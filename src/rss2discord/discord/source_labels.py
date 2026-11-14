from typing import Final
from urllib.parse import parse_qs, urlsplit

from rss2discord.configuration import FeedConfig
from rss2discord.providers.names import ADAPTER_LABELS, STRATEGY_LABELS

SOURCE_LABEL_GITHUB: Final = "GitHub"
SOURCE_LABEL_GITLAB: Final = "GitLab"


def source_label(feed: FeedConfig) -> str:
    if feed.adapter is not None:
        return ADAPTER_LABELS[feed.adapter]
    if feed.strategy == "rss":
        return _rss_source_label(feed.url)
    return STRATEGY_LABELS[feed.strategy]


def _rss_source_label(url: str) -> str:
    try:
        parsed_url = urlsplit(url)
        hostname = parsed_url.hostname
    except ValueError:
        return STRATEGY_LABELS["rss"]
    if hostname is None:
        return STRATEGY_LABELS["rss"]
    hostname_lower = hostname.lower()
    path_segments = tuple(segment for segment in parsed_url.path.split("/") if segment)
    if _is_gitlab_commit_feed(path_segments, parsed_url.query):
        return SOURCE_LABEL_GITLAB
    if (
        hostname_lower == "github.com"
        and len(path_segments) == 3
        and path_segments[-1] == "releases.atom"
    ):
        return SOURCE_LABEL_GITHUB
    if hostname_lower == "news.ycombinator.com":
        return ADAPTER_LABELS["hackernews"]
    if hostname_lower == "reddit.com" or hostname_lower.endswith(".reddit.com"):
        return ADAPTER_LABELS["reddit"]
    return STRATEGY_LABELS["rss"]


def _is_gitlab_commit_feed(path_segments: tuple[str, ...], query: str = "") -> bool:
    """Recognize GitLab's project commit Atom URL shape on any host."""
    query_params = parse_qs(query)
    for marker_index, segment in enumerate(path_segments[:-2]):
        if segment != "-" or path_segments[marker_index + 1] != "commits":
            continue
        if marker_index == 0:
            return False
        branch_segments = path_segments[marker_index + 2 :]
        branch = branch_segments[-1] if branch_segments else ""
        if len(branch) > len(".atom") and branch.casefold().endswith(".atom"):
            return True
        return bool(branch_segments) and "atom" in query_params.get("format", ())
    return False
