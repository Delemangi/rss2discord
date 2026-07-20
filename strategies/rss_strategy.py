"""RSS feed scraping strategy."""

import math
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any, Final

import feedparser
import requests

from models import EntryData, EntryId

from .base import FeedFetchError, ScraperStrategy

MAX_RSS_FEED_BYTES: Final = 1_048_576
RSS_STREAM_CHUNK_BYTES: Final = 65_536


class RSSStrategy(ScraperStrategy):
    """Strategy for scraping RSS/Atom feeds."""

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        """Fetch entries from an RSS feed."""
        try:
            with requests.get(
                url,
                headers={"User-Agent": feedparser.USER_AGENT},
                timeout=30,
                stream=True,
            ) as response:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    status_code = response.status_code
                    raise FeedFetchError(
                        "RSS",
                        "HTTPError",
                        status_code=status_code,
                        retryable=status_code == 429 or 500 <= status_code < 600,
                        retry_after=self._parse_retry_after(
                            response.headers.get("Retry-After"),
                        ),
                    ) from None
                content = self._read_content(response)
        except FeedFetchError:
            raise
        except (requests.ConnectionError, requests.Timeout) as error:
            raise FeedFetchError(
                "RSS",
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError("RSS", type(error).__name__) from None
        feed = feedparser.parse(content)

        if feed.bozo and not feed.entries:
            raise FeedFetchError("RSS", type(feed.bozo_exception).__name__) from None

        feed_title = str(getattr(feed.feed, "title", "RSS Feed"))
        return feed.entries[::-1], feed_title

    @staticmethod
    def _read_content(response: requests.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > MAX_RSS_FEED_BYTES:
                raise FeedFetchError(
                    "RSS",
                    "ResponseTooLarge",
                )

        content = bytearray()
        for chunk in response.iter_content(chunk_size=RSS_STREAM_CHUNK_BYTES):
            if len(content) + len(chunk) > MAX_RSS_FEED_BYTES:
                raise FeedFetchError(
                    "RSS",
                    "ResponseTooLarge",
                )
            content.extend(chunk)
        return bytes(content)

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            retry_after = float(value)
        except ValueError:
            return None
        return retry_after if math.isfinite(retry_after) and retry_after >= 0 else None

    def get_entry_id(self, entry: Any) -> EntryId | None:  # noqa: ANN401
        """Get unique identifier for an RSS entry."""
        for field in ("id", "link"):
            raw_value = entry.get(field)
            if raw_value is not None:
                value = str(raw_value).strip()
                if value:
                    return EntryId(value)
        return None

    def get_entry_data(self, entry: Any) -> EntryData:  # noqa: ANN401
        """Extract data from an RSS entry."""
        title = unescape(str(entry.get("title", "No Title")))
        link = str(entry.get("link", ""))
        author = str(entry.get("author", ""))

        description = str(entry.get("summary", entry.get("description", "")))
        description = self._clean_rss_description(description)
        description = self._truncate(description)

        return EntryData(
            title=title,
            link=link,
            description=description,
            author=author,
            timestamp=self._get_timestamp(entry),
        )

    def _clean_rss_description(self, text: str) -> str:
        """Clean HTML and Reddit-specific markup from RSS description."""
        text = self._clean_html(text)
        text = re.sub(
            r"\s*submitted by\s*/u/\S+.*$",
            "",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        text = re.sub(r"\[link\]|\[comments\]", "", text, flags=re.IGNORECASE)
        return text.strip()

    @staticmethod
    def _get_timestamp(entry: Any) -> str | None:  # noqa: ANN401
        """Get ISO timestamp from RSS entry."""
        for field in ("published_parsed", "updated_parsed"):
            parsed_time = entry.get(field)
            if parsed_time is None:
                continue
            try:
                parsed_datetime = datetime(
                    parsed_time.tm_year,
                    parsed_time.tm_mon,
                    parsed_time.tm_mday,
                    parsed_time.tm_hour,
                    parsed_time.tm_min,
                    parsed_time.tm_sec,
                    tzinfo=UTC,
                )
            except (AttributeError, ValueError):
                continue
            return parsed_datetime.isoformat()
        return None
