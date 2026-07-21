"""RSS feed scraping strategy."""

import math
import re
from collections.abc import Callable, Mapping
from html import unescape
from typing import Any, Final

import feedparser
import requests

from models import EntryData, EntryId

from .base import FeedFetchError, ScraperStrategy
from .rss_timestamp import get_rss_timestamp

MAX_RSS_FEED_BYTES: Final = 1_048_576
RSS_STREAM_CHUNK_BYTES: Final = 65_536
MAX_RSS_CATEGORIES: Final = 3
MAX_RSS_CATEGORY_LENGTH: Final = 64


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

        raw_description = entry.get("summary", entry.get("description", ""))
        description = "" if raw_description is None else str(raw_description)
        if not description.strip():
            description = (
                self._first_structured_value(
                    self._structured_field(entry, "content"),
                    ("value",),
                )
                or ""
            )
        description = self._clean_rss_description(description)
        description = self._truncate(description)

        discussion_url = self._optional_string(
            self._structured_field(entry, "comments"),
        )
        if discussion_url == link.strip():
            discussion_url = None

        image_url = (
            self._first_structured_value(
                self._structured_field(entry, "media_thumbnail"),
                ("url",),
            )
            or self._first_structured_value(
                self._structured_field(entry, "media_content"),
                ("url",),
                image_filter=self._declares_image,
            )
            or self._first_structured_value(
                self._structured_field(entry, "enclosures"),
                ("href", "url"),
                image_filter=self._declares_image_mime,
            )
        )

        return EntryData(
            title=title,
            link=link,
            description=description,
            author=author,
            timestamp=self._get_timestamp(entry),
            discussion_url=discussion_url,
            image_url=image_url,
            categories=self._categories(self._structured_field(entry, "tags")),
        )

    @staticmethod
    def _structured_field(entry: Any, field: str) -> Any:  # noqa: ANN401
        """Read direct feedparser-shaped fields before computed accessors."""
        if isinstance(entry, dict):
            value = dict.get(entry, field)
            if value is not None:
                return value
        return entry.get(field)

    @staticmethod
    def _optional_string(value: Any) -> str | None:  # noqa: ANN401
        """Return a non-empty, trimmed feedparser scalar string."""
        if not isinstance(value, str):
            return None
        normalized_value = value.strip()
        return normalized_value or None

    @classmethod
    def _first_structured_value(
        cls,
        items: Any,  # noqa: ANN401
        fields: tuple[str, ...],
        *,
        image_filter: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> str | None:
        """Return the first usable string from list-shaped structured metadata."""
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if image_filter is not None and not image_filter(item):
                continue
            for field in fields:
                value = cls._optional_string(item.get(field))
                if value is not None:
                    return value
        return None

    @staticmethod
    def _declares_image(item: Mapping[str, Any]) -> bool:
        """Determine whether a media-content item declares an image."""
        medium = item.get("medium")
        media_type = item.get("type")
        return (isinstance(medium, str) and medium.casefold() == "image") or (
            isinstance(media_type, str) and media_type.casefold().startswith("image/")
        )

    @staticmethod
    def _declares_image_mime(item: Mapping[str, Any]) -> bool:
        """Determine whether an enclosure item declares an image MIME type."""
        media_type = item.get("type")
        return isinstance(media_type, str) and media_type.casefold().startswith(
            "image/",
        )

    @classmethod
    def _categories(cls, tags: Any) -> tuple[str, ...]:  # noqa: ANN401
        """Normalize bounded category terms from list-shaped feedparser tags."""
        if not isinstance(tags, list):
            return ()

        categories: list[str] = []
        for tag in tags:
            if not isinstance(tag, Mapping):
                continue
            category = cls._optional_string(tag.get("term"))
            if category is None:
                continue
            category = " ".join(category.split())
            category = category[:MAX_RSS_CATEGORY_LENGTH]
            if category in categories:
                continue
            categories.append(category)
            if len(categories) == MAX_RSS_CATEGORIES:
                break
        return tuple(categories)

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

    @classmethod
    def _get_timestamp(cls, entry: Any) -> str | None:  # noqa: ANN401
        """Get ISO timestamp from RSS entry."""
        return get_rss_timestamp(entry, cls._parse_timestamp)
