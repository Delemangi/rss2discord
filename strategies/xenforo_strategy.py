"""XenForo forum scraping strategy."""

import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

from forumscraper import Outputs, xenforo  # type: ignore[import-untyped]

from .base import ScraperStrategy

logger = logging.getLogger(__name__)


class XenForoStrategy(ScraperStrategy):
    """Strategy for scraping XenForo forums."""

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        """
        Fetch posts from a XenForo forum thread.

        Args:
            url: The forum thread URL

        Returns:
            A tuple of (posts list, thread title)
        """
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                original_cwd = Path.cwd()
                try:
                    os.chdir(tempdir)
                    scraper = xenforo(
                        output=Outputs.data | Outputs.write_by_id,
                    )
                    result = scraper.get_thread(url)
                finally:
                    os.chdir(original_cwd)

            if not result or not isinstance(result, dict):
                error_msg = f"Failed to fetch XenForo thread from {url}"
                logger.error(error_msg)
                raise ValueError(error_msg)  # noqa: TRY301

            threads = result.get("data", {}).get("threads", [])
            thread = threads[0] if threads else {}

            title = thread.get("title", "XenForo Thread")
            thread_url = thread.get("url")
            posts = thread.get("posts", [])

            if posts:
                for post in posts:
                    if isinstance(post, dict):
                        post["title"] = title
                        if thread_url:
                            post["thread_url"] = thread_url

        except Exception as e:
            error_msg = f"Error scraping XenForo forum: {e}"
            logger.exception(error_msg)
            raise ValueError(error_msg) from e
        else:
            return posts, title

    def get_entry_id(self, entry: Any) -> str:  # noqa: ANN401
        """
        Get unique identifier for a forum post.

        Args:
            entry: The forum post object (dict)

        Returns:
            A unique identifier string
        """
        if isinstance(entry, dict) and "id" in entry:
            return str(entry["id"])

        return str(hash(str(entry)))

    def get_entry_data(self, entry: Any) -> dict[str, Any]:  # noqa: ANN401
        """
        Extract data from a forum post.

        Args:
            entry: The forum post object (dict)

        Returns:
            Dictionary with keys: title, link, description, author, timestamp
        """
        if not isinstance(entry, dict):
            entry = {"content": str(entry)}

        # Extract post data
        author = entry.get("author", entry.get("user", "Unknown"))
        title = entry.get("title", "XenForo Thread")

        thread_url = entry.get("thread_url")
        id = entry.get("id")
        url = f"{thread_url}post-{id}" if thread_url and id is not None else ""

        content = entry.get("content", entry.get("text", ""))

        content = self._clean_content(content)

        if len(content) > 2000:
            content = content[:1997] + "..."

        return {
            "title": title,
            "link": url,
            "description": content,
            "author": author,
            "timestamp": self._get_timestamp(entry),
        }

    def _clean_content(self, text: str) -> str:
        """Clean HTML tags and unwanted content from post content."""
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
        text = text.replace("Кликни за повеќе...", "")
        text = text.strip()

        return text

    def _get_timestamp(self, entry: Any) -> str:  # noqa: ANN401
        """Get ISO timestamp from post."""
        timestamp = self._extract_timestamp(entry)

        if timestamp:
            return self._convert_timestamp(timestamp)

        return datetime.now(UTC).isoformat()

    def _extract_timestamp(self, entry: Any) -> Any:  # noqa: ANN401
        """Extract timestamp from entry attributes."""
        if not isinstance(entry, dict):
            return None

        for field in ["timestamp", "created_at", "date", "posted_at", "time"]:
            if field in entry:
                return entry[field]

        return None

    def _convert_timestamp(self, timestamp: Any) -> str:  # noqa: ANN401
        """Convert various timestamp formats to ISO format."""
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
