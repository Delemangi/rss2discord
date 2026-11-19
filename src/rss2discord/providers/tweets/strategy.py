"""Generic tweets strategy backed by a Nitter RSS profile feed."""

import logging
import re
from dataclasses import replace
from typing import Any, Final, final, override
from urllib.parse import urlsplit, urlunsplit

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import EntryData, EntryId
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.rss import RSSStrategy

logger = logging.getLogger(__name__)

TWEETS_FALLBACK_LABEL: Final = "Tweets"
TWEET_URL: Final = "https://x.com/{handle}/status/{entry_id}"
EXCLUDED_TITLE_PREFIXES: Final = ("R to @", "RT by @", "Pinned: ")
HANDLE_PATTERN: Final = re.compile(r"[A-Za-z0-9_]{1,15}")
NITTER_FALLBACK_HOSTS: Final = (
    "nitter.kareem.one",
    "nitter.mie.foundation",
    "nitter.jaydenha.uk",
    "ruf.st",
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
        entry_id = self._rss.get_entry_id(entry)
        if entry_id is None or not entry_id.isdigit():
            return data
        handle = self._handle_from_link(data.link)
        if handle is None:
            return data
        return replace(
            data,
            link=TWEET_URL.format(handle=handle, entry_id=entry_id),
        )

    @staticmethod
    def _handle_from_link(link: str) -> str | None:
        try:
            parts = [part for part in urlsplit(link).path.split("/") if part]
        except ValueError:
            return None
        if len(parts) != 3 or parts[1] != "status":
            return None
        handle = parts[0]
        return handle if HANDLE_PATTERN.fullmatch(handle) else None

    @staticmethod
    def _is_excluded(entry: Any) -> bool:  # noqa: ANN401
        title = str(entry.get("title", "")).strip()
        return title.startswith(EXCLUDED_TITLE_PREFIXES)
