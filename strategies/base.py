"""Base strategy interface for content scraping."""

from abc import ABC, abstractmethod
from typing import Any


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
