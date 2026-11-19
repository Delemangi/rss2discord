"""Generic tweets strategy backed by a Nitter RSS profile feed."""

import logging
import re
from dataclasses import replace
from typing import Any, Final, final, override
from urllib.parse import unquote, urlsplit, urlunsplit

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, EntryId
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.rss import RSSStrategy
from rss2discord.url_normalization import normalize_remote_media_url

logger = logging.getLogger(__name__)

TWEETS_FALLBACK_LABEL: Final = "Tweets"
TWEETS_MEDIA_TITLE: Final = "Media tweet"
EXCLUDED_TITLE_PREFIXES: Final = ("R to @", "RT by @", "Pinned: ")
EMPTY_TITLES: Final = frozenset({"", "...", "…", "No Title"})
NITTER_STATUS_LINK: Final = re.compile(
    r"(?:\s*[^\w\s]?\s*)?https?://[^\s/]+/[^\s/]+/status/\d+(?:#m)?\s*$",
    re.IGNORECASE,
)
NITTER_MEDIA_LABEL: Final = re.compile(
    r"(?m)^\s*(?:Video|Image)\s*$",
    re.IGNORECASE,
)
NITTER_FALLBACK_HOSTS: Final = (
    "nitter.kareem.one",
    "nitter.mie.foundation",
    "nitter.jaydenha.uk",
    "ruf.st",
)
ATTACHED_MEDIA_PATH_HINTS: Final = (
    "/pic/media",
    "/pic/amplify_video_thumb",
    "/pic/ext_tw_video_thumb",
    "/pic/tweet_video_thumb",
)
_LOGGED_FAILOVERS: set[tuple[str, str]] = set()


def _candidate_urls(url: str) -> tuple[str, ...]:
    """Return the configured Nitter URL followed by known fallback instances."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return (url,)
    host = parsed.hostname
    if host is None:
        return (url,)
    host = host.casefold()
    candidates = [url]
    for fallback_host in NITTER_FALLBACK_HOSTS:
        if fallback_host == host:
            continue
        netloc = fallback_host
        if port is not None:
            netloc = f"{fallback_host}:{port}"
        candidates.append(urlunsplit(parsed._replace(netloc=netloc)))
    return tuple(candidates)


def _log_failover(url: str, candidate: str) -> None:
    pair = (url, candidate)
    if pair in _LOGGED_FAILOVERS:
        return
    _LOGGED_FAILOVERS.add(pair)
    logger.warning("Nitter instance failed over: %s -> %s", url, candidate)


def _public_tweet_link(link: str) -> str:
    """Keep the public Nitter tweet link and drop its media fragment."""
    try:
        parsed = urlsplit(link)
    except ValueError:
        return link
    if parsed.scheme.casefold() == "http":
        parsed = parsed._replace(scheme="https")
    return urlunsplit(parsed._replace(fragment=""))


def _canonical_tweet_media_url(url: str) -> str:
    """Prefer Twitter's CDN over a Nitter instance's image proxy."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not parsed.path.startswith("/pic/"):
        return url
    decoded = unquote(parsed.path[len("/pic/") :])
    if (
        not decoded
        or decoded.startswith(("http://", "https://"))
        or ".." in decoded
        or "\\" in decoded
    ):
        return url
    canonical = normalize_remote_media_url(f"https://pbs.twimg.com/{decoded}")
    return canonical or url


def _tweet_image_urls(entry: Any, rss: RSSStrategy) -> tuple[str, ...]:  # noqa: ANN401
    raw_html = entry.get("summary", entry.get("description", ""))
    if not raw_html:
        return ()
    urls = rss.inline_image_urls(str(raw_html), upgrade_http=True)
    attached = tuple(url for url in urls if _is_attached_media(url))
    return tuple(_canonical_tweet_media_url(url) for url in (attached or urls))


def _is_attached_media(url: str) -> bool:
    path = urlsplit(url).path
    return any(hint in path for hint in ATTACHED_MEDIA_PATH_HINTS)


def _clean_description(description: str) -> str:
    """Drop Nitter-only media labels, status links, and excess blank lines."""
    cleaned = NITTER_STATUS_LINK.sub("", description)
    cleaned = NITTER_MEDIA_LABEL.sub("", cleaned)
    cleaned = re.sub(r"\s\ufffd\s", " · ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _fallback_title(title: str, description: str) -> str:
    stripped = title.strip()
    if stripped not in EMPTY_TITLES:
        return stripped
    first_line = next(
        (line.strip() for line in description.splitlines() if line.strip()),
        "",
    )
    return first_line[:120] if first_line else TWEETS_MEDIA_TITLE


@final
class TweetsStrategy(ScraperStrategy):
    """Keep original tweets from a Nitter profile feed."""

    seed_existing_on_first_fetch = True

    def __init__(self) -> None:
        self._rss = RSSStrategy()

    @override
    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        last_error: FeedFetchError | None = None
        for candidate in _candidate_urls(url):
            try:
                entries, source_title = self._rss.fetch_entries(candidate)
            except FeedFetchInterruptedError:
                raise
            except FeedFetchError as error:
                if not error.retryable:
                    raise
                last_error = error
                continue
            if candidate != url:
                _log_failover(url, candidate)
            filtered = [entry for entry in entries if not self._is_excluded(entry)]
            return filtered, source_title or TWEETS_FALLBACK_LABEL
        if last_error is None:
            raise FeedFetchError("Tweets", "NoNitterInstance")
        raise last_error

    @override
    def get_entry_id(self, entry: Any) -> EntryId | None:
        return self._rss.get_entry_id(entry)

    @override
    def get_entry_data(self, entry: Any) -> EntryData:
        data = self._rss.get_entry_data(entry)
        description = _clean_description(data.description)
        images = _tweet_image_urls(entry, self._rss)
        if not images and data.image_url is not None:
            images = (data.image_url,)
        return replace(
            data,
            title=_fallback_title(data.title, description),
            description=description,
            link=_public_tweet_link(data.link),
            image_url=images[0] if images else None,
            image_urls=images,
        )

    @staticmethod
    def _is_excluded(entry: Any) -> bool:  # noqa: ANN401
        title = str(entry.get("title", "")).strip()
        return title.startswith(EXCLUDED_TITLE_PREFIXES)
