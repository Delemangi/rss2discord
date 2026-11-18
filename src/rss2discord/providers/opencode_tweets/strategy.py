"""OpenCode tweets strategy backed by a Nitter RSS mirror."""

from dataclasses import replace
from typing import Any, Final, final, override

from rss2discord.models import EntryData, EntryId
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.rss import RSSStrategy

OPENCODE_TWEETS_LABEL: Final = "OpenCode / @opencode"
OPENCODE_TWEET_URL: Final = "https://x.com/opencode/status/"
EXCLUDED_TITLE_PREFIXES: Final = ("R to ", "RT by ", "Pinned: ")


@final
class OpenCodeTweetsStrategy(ScraperStrategy):
    """Keep original @opencode tweets from a Nitter profile feed."""

    seed_existing_on_first_fetch = True

    def __init__(self) -> None:
        self._rss = RSSStrategy()

    @override
    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        entries, _source_title = self._rss.fetch_entries(url)
        return [
            entry for entry in entries if not self._is_excluded(entry)
        ], OPENCODE_TWEETS_LABEL

    @override
    def get_entry_id(self, entry: Any) -> EntryId | None:
        return self._rss.get_entry_id(entry)

    @override
    def get_entry_data(self, entry: Any) -> EntryData:
        data = self._rss.get_entry_data(entry)
        entry_id = self._rss.get_entry_id(entry)
        if entry_id is None or not entry_id.isdigit():
            return data
        return replace(data, link=f"{OPENCODE_TWEET_URL}{entry_id}")

    @staticmethod
    def _is_excluded(entry: Any) -> bool:  # noqa: ANN401
        title = str(entry.get("title", "")).strip()
        return title.startswith(EXCLUDED_TITLE_PREFIXES)
