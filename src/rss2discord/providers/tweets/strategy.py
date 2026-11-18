"""Generic tweets strategy backed by a Nitter RSS profile feed."""

import re
from dataclasses import replace
from typing import Any, Final, final, override
from urllib.parse import urlsplit

from rss2discord.models import EntryData, EntryId
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.rss import RSSStrategy

TWEETS_FALLBACK_LABEL: Final = "Tweets"
TWEET_URL: Final = "https://x.com/{handle}/status/{entry_id}"
EXCLUDED_TITLE_PREFIXES: Final = ("R to @", "RT by @", "Pinned: ")
HANDLE_PATTERN: Final = re.compile(r"[A-Za-z0-9_]{1,15}")


@final
class TweetsStrategy(ScraperStrategy):
    """Keep original tweets from a Nitter profile feed."""

    seed_existing_on_first_fetch = True

    def __init__(self) -> None:
        self._rss = RSSStrategy()

    @override
    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        entries, source_title = self._rss.fetch_entries(url)
        filtered = [entry for entry in entries if not self._is_excluded(entry)]
        return filtered, source_title or TWEETS_FALLBACK_LABEL

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
