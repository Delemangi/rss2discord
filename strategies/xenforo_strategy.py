"""XenForo forum scraping strategy."""

import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forumscraper import Outputs, xenforo  # type: ignore[import-untyped]

from .base import ScraperStrategy

logger = logging.getLogger(__name__)


class XenForoStrategy(ScraperStrategy):
    """Strategy for scraping XenForo forums."""

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        """Fetch posts from a XenForo forum thread."""
        try:
            with tempfile.TemporaryDirectory() as tempdir:
                original_cwd = Path.cwd()
                try:
                    os.chdir(tempdir)
                    scraper = xenforo(
                        output=Outputs.data | Outputs.write_by_id,
                        requests={
                            "allow_redirects": True,
                        },
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
        """Get unique identifier for a forum post."""
        if isinstance(entry, dict) and "id" in entry:
            return str(entry["id"])

        return str(hash(str(entry)))

    def get_entry_data(self, entry: Any) -> dict[str, Any]:  # noqa: ANN401
        """Extract data from a forum post."""
        if not isinstance(entry, dict):
            entry = {"content": str(entry)}

        author = entry.get("author", entry.get("user", "Unknown"))
        title = entry.get("title", "XenForo Thread")

        thread_url = entry.get("thread_url")
        post_id = entry.get("id")
        link = (
            f"{thread_url}post-{post_id}" if thread_url and post_id is not None else ""
        )

        content = entry.get("content", entry.get("text", ""))
        content = self._clean_xenforo_content(content)
        content = self._truncate(content)

        return {
            "title": title,
            "link": link,
            "description": content,
            "author": author,
            "timestamp": self._get_timestamp(entry),
        }

    def _clean_xenforo_content(self, text: str) -> str:
        """Clean HTML and XenForo-specific markup from post content."""
        text = self._clean_html(text)
        text = text.replace("Кликни за повеќе...", "")
        return text.strip()

    def _get_timestamp(self, entry: Any) -> str:  # noqa: ANN401
        """Get ISO timestamp from a forum post."""
        if isinstance(entry, dict):
            for field in ("timestamp", "created_at", "date", "posted_at", "time"):
                if field in entry:
                    return self._parse_timestamp(entry[field])

        return datetime.now(UTC).isoformat()
