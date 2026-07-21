import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from html import unescape
from typing import Any

from rss2discord.models import EntryData, EntryId

MAX_DESCRIPTION_LENGTH = 2000


class FeedFetchError(Exception):
    def __init__(
        self,
        strategy: str,
        cause_type: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        self.strategy = strategy
        self.cause_type = cause_type
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        detail = f"HTTP {status_code}" if status_code is not None else cause_type
        super().__init__(f"{strategy} fetch failed ({detail})")


class ScraperStrategy(ABC):
    @abstractmethod
    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        """
        Fetch entries from the given URL.

        Args:
            url: The URL to scrape

        Returns:
            A tuple of (entries list, source title)
        """

    @abstractmethod
    def get_entry_id(self, entry: Any) -> EntryId | None:  # noqa: ANN401
        """
        Get the stable typed identifier for an entry.

        Args:
            entry: The entry object

        Returns:
            The entry ID, or None when no stable identifier is available
        """

    @abstractmethod
    def get_entry_data(self, entry: Any) -> EntryData:  # noqa: ANN401
        """
        Extract an EntryData value for a Discord webhook.

        Args:
            entry: The entry object

        Returns:
            The normalized entry data
        """

    @staticmethod
    def _clean_html(text: str) -> str:
        if not text:
            return ""

        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        text = text.replace("&#32;", " ")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _truncate(text: str, max_length: int = MAX_DESCRIPTION_LENGTH) -> str:
        if len(text) > max_length:
            return text[: max_length - 3] + "..."
        return text

    @staticmethod
    def _parse_timestamp(timestamp: Any) -> str | None:  # noqa: ANN401
        """Convert various timestamp formats to ISO format string.

        Supports datetime objects, ISO format strings, and numeric
        (Unix epoch) timestamps. Returns None when conversion fails.
        """
        match timestamp:
            case datetime() as parsed_datetime:
                if parsed_datetime.tzinfo is None:
                    parsed_datetime = parsed_datetime.replace(tzinfo=UTC)
                return parsed_datetime.isoformat()
            case str() as timestamp_string:
                try:
                    parsed_datetime = datetime.fromisoformat(timestamp_string)
                except ValueError:
                    return None
                if parsed_datetime.tzinfo is None:
                    parsed_datetime = parsed_datetime.replace(tzinfo=UTC)
                return parsed_datetime.isoformat()
            case int() | float() as epoch:
                try:
                    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
                except (OSError, OverflowError, ValueError):
                    return None
            case _:
                return None
