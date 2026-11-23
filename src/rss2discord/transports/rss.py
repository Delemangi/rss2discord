"""RSS feed scraping strategy."""

import re
from collections.abc import Callable, Mapping
from html import unescape
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

from rss2discord.models import EntryData, EntryId
from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError, ScraperStrategy
from rss2discord.transports.bounded_http import read_bounded_content
from rss2discord.transports.rss_timestamp import get_rss_timestamp
from rss2discord.url_normalization import (
    normalize_http_url,
    normalize_remote_media_url,
)

MAX_RSS_FEED_BYTES: Final = 1_048_576
GITHUB_RELEASE_ATOM_MAX_BYTES: Final = 4_194_304
RSS_STREAM_CHUNK_BYTES: Final = 65_536
MAX_RSS_CATEGORIES: Final = 3
MAX_RSS_CATEGORY_LENGTH: Final = 64
MAX_RSS_INLINE_IMAGE_CANDIDATES: Final = 10
INLINE_IMAGE_TAG: Final = re.compile(r"<img\b[^>]{0,4096}>", re.IGNORECASE)
INLINE_IMAGE_ATTRIBUTE: Final = re.compile(
    r"""(?:^|\s)(?P<name>src|data-src|srcset|width|height|style)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s"'=<>`]+))""",
    re.IGNORECASE,
)
PLACEHOLDER_IMAGE_HINTS: Final = (
    "blank.gif",
    "spacer.gif",
    "pixel.gif",
    "1x1.",
    "transparent.gif",
)


def _is_github_release_atom_url(url: str) -> bool:
    """Match only plain public GitHub owner/repository release Atom URLs."""
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.netloc == "github.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(
                r"/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/releases\.atom",
                parsed.path,
            )
            is not None
            and ".." not in parsed.path.split("/")
        )
    except ValueError:
        return False


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
                max_bytes = MAX_RSS_FEED_BYTES
                final_url = getattr(response, "url", None)
                if (
                    _is_github_release_atom_url(url)
                    and isinstance(final_url, str)
                    and _is_github_release_atom_url(final_url)
                ):
                    max_bytes = GITHUB_RELEASE_ATOM_MAX_BYTES
                content = read_bounded_content(
                    response,
                    label="RSS",
                    max_bytes=max_bytes,
                    chunk_bytes=RSS_STREAM_CHUNK_BYTES,
                )
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
    def _parse_retry_after(value: str | None) -> float | None:
        return parse_retry_after(value)

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
        link = normalize_http_url(str(entry.get("link", ""))) or ""
        author = str(entry.get("author", ""))

        raw_description = entry.get("summary", entry.get("description", ""))
        description = "" if raw_description is None else str(raw_description)
        structured_content = self._first_structured_value(
            self._structured_field(entry, "content"),
            ("value",),
        )
        if not description.strip():
            description = structured_content or ""
        raw_html = description
        description = self._clean_rss_description(description)
        description = self._truncate(description)

        raw_discussion_url = self._optional_string(
            self._structured_field(entry, "comments"),
        )
        discussion_url = (
            normalize_http_url(raw_discussion_url)
            if raw_discussion_url is not None
            else None
        )
        if discussion_url == link:
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
        if image_url is None:
            image_url = self._inline_image_url(raw_html)
        if image_url is None and structured_content and structured_content != raw_html:
            image_url = self._inline_image_url(structured_content)

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
    def inline_image_urls(
        cls,
        html: str,
        *,
        upgrade_http: bool = False,
    ) -> tuple[str, ...]:
        """Return safe inline images from an entry description, in order."""
        if not html:
            return ()
        urls: list[str] = []
        for index, match in enumerate(INLINE_IMAGE_TAG.finditer(html)):
            if index >= MAX_RSS_INLINE_IMAGE_CANDIDATES:
                break
            attributes = cls._inline_image_attributes(match.group(0))
            if cls._is_tracking_pixel(attributes):
                continue
            normalized = cls._resolved_inline_image_url(
                attributes,
                upgrade_http=upgrade_http,
            )
            if normalized is not None and normalized not in urls:
                urls.append(normalized)
        return tuple(urls)

    @classmethod
    def _resolved_inline_image_url(
        cls,
        attributes: Mapping[str, str],
        *,
        upgrade_http: bool,
    ) -> str | None:
        """Return the first safe URL declared by one inline image tag."""
        for name in ("src", "data-src", "srcset"):
            raw_url = attributes.get(name)
            if raw_url is None:
                continue
            if name == "srcset":
                raw_url = raw_url.split(",", 1)[0].strip().split(" ", 1)[0]
            normalized = normalize_remote_media_url(unescape(raw_url))
            if normalized is None and upgrade_http:
                normalized = cls._upgrade_http_media_url(raw_url)
            if normalized is None or cls._is_placeholder_image_url(normalized):
                continue
            return normalized
        return None

    @classmethod
    def _inline_image_url(cls, html: str) -> str | None:
        """Return the first safe inline image from an entry description."""
        urls = cls.inline_image_urls(html)
        return urls[0] if urls else None

    @staticmethod
    def _upgrade_http_media_url(value: str) -> str | None:
        normalized = normalize_http_url(unescape(value))
        if normalized is None:
            return None
        parsed = urlsplit(normalized)
        if parsed.scheme.casefold() != "http":
            return None
        return normalize_remote_media_url(urlunsplit(parsed._replace(scheme="https")))

    @staticmethod
    def _inline_image_attributes(tag: str) -> dict[str, str]:
        """Read the first value of each supported attribute in one image tag."""
        attributes: dict[str, str] = {}
        for match in INLINE_IMAGE_ATTRIBUTE.finditer(tag):
            name = match.group("name").casefold()
            value = (
                match.group("double")
                or match.group("single")
                or match.group("bare")
                or ""
            )
            attributes.setdefault(name, value)
        return attributes

    @staticmethod
    def _is_tracking_pixel(attributes: Mapping[str, str]) -> bool:
        """Reject one- or two-pixel images and their inline-style equivalents."""
        for name in ("width", "height"):
            value = attributes.get(name)
            if value is None:
                continue
            dimension = re.match(r"\s*(\d+)", value)
            if dimension is not None and int(dimension.group(1)) <= 2:
                return True
        style = attributes.get("style", "")
        return (
            re.search(
                r"(?:width|height)\s*:\s*[12](?:px)?\b",
                style,
                flags=re.IGNORECASE,
            )
            is not None
        )

    @staticmethod
    def _is_placeholder_image_url(url: str) -> bool:
        """Reject common lazy-load and tracking placeholder filenames."""
        lowered = url.casefold()
        return any(hint in lowered for hint in PLACEHOLDER_IMAGE_HINTS)

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

    def clean_description(self, text: str) -> str:
        """Clean provider-supplied summary HTML with the RSS rules."""
        return self._clean_rss_description(text)

    def _clean_rss_description(self, text: str) -> str:
        """Clean HTML and Reddit-specific markup from RSS description."""
        text = self._clean_html(text)
        parts: list[str] = []
        end = 0
        for match in re.finditer(
            r"submitted by\s*/u/\S[^\n]*",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            # Trim the preceding whitespace without retrying a regex at every
            # position in a potentially very long whitespace-only prefix.
            parts.append(text[end : match.start()].rstrip())
            end = match.end()
        parts.append(text[end:])
        text = "".join(parts)
        text = re.sub(r"\[link\]|\[comments\]", "", text, flags=re.IGNORECASE)
        return text.strip()

    @classmethod
    def _get_timestamp(cls, entry: Any) -> str | None:  # noqa: ANN401
        """Get ISO timestamp from RSS entry."""
        return get_rss_timestamp(entry, cls._parse_timestamp)
