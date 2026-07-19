"""Base strategy interface for content scraping."""

import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from html import unescape
from typing import Any

from models import EntryData, EntryId

MAX_DESCRIPTION_LENGTH = 2000


class ScraperStrategy(ABC):
    """Abstract base class for scraping strategies."""

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
        Get unique identifier for an entry.

        Args:
            entry: The entry object

        Returns:
            A unique identifier string
        """

    @abstractmethod
    def get_entry_data(self, entry: Any) -> EntryData:  # noqa: ANN401
        """
        Extract data from an entry for Discord webhook.

        Args:
            entry: The entry object

        Returns:
            Dictionary with keys: title, link, description, author, timestamp
        """

    @staticmethod
    def _clean_html(text: str) -> str:
        """Clean HTML tags and common unwanted markup from text.

        Handles comments, line break tags, paragraph tags, remaining tags,
        HTML entities, and excessive whitespace.
        """
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
        """Truncate text to max_length, appending '...' if truncated."""
        if len(text) > max_length:
            return text[: max_length - 3] + "..."
        return text

    @staticmethod
    def _parse_timestamp(timestamp: Any) -> str | None:  # noqa: ANN401
        """Convert various timestamp formats to ISO format string.

        Supports datetime objects, ISO format strings, and numeric
        (Unix epoch) timestamps. Falls back to current UTC time.
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
