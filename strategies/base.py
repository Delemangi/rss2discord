"""Base strategy interface for content scraping."""

import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from html import unescape
from typing import Any

logger = logging.getLogger(__name__)

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
    def get_entry_id(self, entry: Any) -> str:  # noqa: ANN401
        """
        Get unique identifier for an entry.

        Args:
            entry: The entry object

        Returns:
            A unique identifier string
        """

    @abstractmethod
    def get_entry_data(self, entry: Any) -> dict[str, Any]:  # noqa: ANN401
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
    def _parse_timestamp(timestamp: Any) -> str:  # noqa: ANN401
        """Convert various timestamp formats to ISO format string.

        Supports datetime objects, ISO format strings, and numeric
        (Unix epoch) timestamps. Falls back to current UTC time.
        """
        if isinstance(timestamp, datetime):
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return timestamp.isoformat()

        if isinstance(timestamp, str):
            try:
                dt = datetime.fromisoformat(timestamp)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.isoformat()
            except ValueError:
                logger.debug("Could not parse timestamp string: %s", timestamp)

        if isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp, tz=UTC)
            return dt.isoformat()

        logger.debug("Unrecognized timestamp format: %s", type(timestamp))
        return datetime.now(UTC).isoformat()
