"""Bounded Time.mk category-feed aggregation."""

import logging
import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from typing import Final
from urllib.parse import urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

from rss2discord.models import EntryId
from rss2discord.providers.time_mk.bounds import (
    MAX_TIME_MK_CATEGORIES,
    MAX_TIME_MK_DESCRIPTION_LENGTH,
    MAX_TIME_MK_DETAIL_BYTES,
    MAX_TIME_MK_IMAGE_URL_LENGTH,
    MAX_TIME_MK_RESPONSE_BYTES,
    MAX_TIME_MK_SCAN_BYTES,
    MAX_TIME_MK_SCAN_SECONDS,
    TIME_MK_ALL_CATEGORIES,
    TIME_MK_ALL_FEED,
    TIME_MK_DEFAULT_CATEGORIES,
    TIME_MK_DETAIL_SECONDS,
    TIME_MK_HOSTS,
    TIME_MK_LABEL,
    TIME_MK_ORIGIN,
    TIME_MK_REQUEST_SECONDS,
    TIME_MK_RSS_PATH,
    TIME_MK_STREAM_CHUNK_BYTES,
    TIME_MK_USER_AGENT,
)
from rss2discord.providers.time_mk.models import (
    TimeMkArticle,
    TimeMkArticleDetails,
)
from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError

TIME_MK_TERMINAL_CAUSES: Final = frozenset({"ScanTimeout", "ScanResponseTooLarge"})
TIME_MK_ARTICLE_PATH: Final = re.compile(r"/c/([0-9a-f]+)/")
MAX_TIME_MK_TITLE_LENGTH: Final = 500
MAX_TIME_MK_SOURCE_LENGTH: Final = 200
MAX_TIME_MK_CATEGORY_LENGTH: Final = 256

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _FetchedCategory:
    articles: tuple[TimeMkArticle, ...]
    response_bytes: int


class TimeMkCatalogClient:
    """Fetch a configurable subset of Time.mk category feeds."""

    def fetch_articles(self, url: str) -> tuple[TimeMkArticle, ...]:
        categories = normalize_time_mk_url(url)
        deadline = time.monotonic() + MAX_TIME_MK_SCAN_SECONDS
        remaining_bytes = MAX_TIME_MK_SCAN_BYTES
        articles: dict[EntryId, TimeMkArticle] = {}
        successes = 0
        for category in categories:
            try:
                fetched = self._fetch_category(category, deadline, remaining_bytes)
            except FeedFetchError as error:
                if error.cause_type in TIME_MK_TERMINAL_CAUSES:
                    raise
                logger.warning(
                    "Time.mk category fetch failed: category=%s cause=%s; "
                    "continuing best-effort scan",
                    category,
                    error.cause_type,
                )
                continue
            successes += 1
            remaining_bytes -= fetched.response_bytes
            for article in fetched.articles:
                articles.setdefault(article.entry_id, article)
        if successes == 0:
            raise FeedFetchError(TIME_MK_LABEL, "EmptyResponse")
        return _sorted_articles(articles.values())

    def fetch_details(self, url: str) -> TimeMkArticleDetails | None:
        """Read the Open Graph summary and image from one Time.mk article page."""
        if not _is_time_mk_article_url(url):
            return None
        try:
            with requests.get(
                url,
                headers={"User-Agent": TIME_MK_USER_AGENT},
                timeout=TIME_MK_DETAIL_SECONDS,
                stream=True,
            ) as response:
                response.raise_for_status()
                content = _read_content(
                    response,
                    remaining_scan_bytes=MAX_TIME_MK_DETAIL_BYTES,
                )
        except FeedFetchError as error:
            logger.warning(
                "Time.mk article detail fetch failed: cause=%s",
                error.cause_type,
            )
            return None
        except requests.RequestException as error:
            logger.warning(
                "Time.mk article detail fetch failed: cause=%s",
                type(error).__name__,
            )
            return None
        soup = BeautifulSoup(content, "html.parser")
        return TimeMkArticleDetails(
            description=_clean_text(
                _meta_content(soup, "og:description"),
                MAX_TIME_MK_DESCRIPTION_LENGTH,
            ),
            image_url=_safe_image_url(_meta_content(soup, "og:image")),
        )

    def _fetch_category(
        self,
        category: str,
        deadline: float,
        remaining_bytes: int,
    ) -> _FetchedCategory:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise FeedFetchError(TIME_MK_LABEL, "ScanTimeout")
        if remaining_bytes <= 0:
            raise FeedFetchError(TIME_MK_LABEL, "ScanResponseTooLarge")
        url = f"{TIME_MK_ORIGIN}{TIME_MK_RSS_PATH}{category}"
        try:
            with requests.get(
                url,
                headers={"User-Agent": TIME_MK_USER_AGENT},
                timeout=max(
                    1,
                    min(TIME_MK_REQUEST_SECONDS, math.ceil(remaining_seconds)),
                ),
                stream=True,
            ) as response:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    status = response.status_code
                    raise FeedFetchError(
                        TIME_MK_LABEL,
                        "HTTPError",
                        status_code=status,
                        retryable=status in {408, 429} or 500 <= status < 600,
                        retry_after=parse_retry_after(
                            response.headers.get("Retry-After"),
                        ),
                    ) from None
                content = _read_content(
                    response,
                    remaining_scan_bytes=remaining_bytes,
                )
        except FeedFetchError:
            raise
        except (requests.ConnectionError, requests.Timeout) as error:
            raise FeedFetchError(
                TIME_MK_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(TIME_MK_LABEL, type(error).__name__) from None
        feed = feedparser.parse(content)
        if feed.bozo and not feed.entries:
            raise FeedFetchError(TIME_MK_LABEL, type(feed.bozo_exception).__name__)
        articles = tuple(
            article
            for entry in feed.entries
            if (article := _article_from_entry(entry)) is not None
        )
        return _FetchedCategory(articles, len(content))


def normalize_time_mk_url(url: str) -> tuple[str, ...]:
    """Map one Time.mk URL to the ordered categories it should fetch."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(TIME_MK_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in TIME_MK_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(TIME_MK_LABEL, "InvalidUrl")
    path = parsed.path.rstrip("/") or "/"
    if path == "/":
        return TIME_MK_DEFAULT_CATEGORIES
    if path == "/all":
        return TIME_MK_ALL_CATEGORIES
    if not path.startswith(TIME_MK_RSS_PATH):
        raise FeedFetchError(TIME_MK_LABEL, "InvalidUrl")
    raw_categories = path.removeprefix(TIME_MK_RSS_PATH)
    if raw_categories == TIME_MK_ALL_FEED:
        return TIME_MK_ALL_CATEGORIES
    categories = tuple(
        dict.fromkeys(category for category in raw_categories.split(",") if category),
    )
    if not categories or len(categories) > MAX_TIME_MK_CATEGORIES:
        raise FeedFetchError(TIME_MK_LABEL, "InvalidUrl")
    for category in categories:
        if category != TIME_MK_ALL_FEED and category not in TIME_MK_ALL_CATEGORIES:
            raise FeedFetchError(TIME_MK_LABEL, "UnknownCategory")
    return categories


def _article_from_entry(entry: dict[str, object]) -> TimeMkArticle | None:
    raw_title = entry.get("title")
    raw_link = entry.get("link")
    if not isinstance(raw_title, str) or not isinstance(raw_link, str):
        return None
    link = raw_link.strip()
    if not _is_time_mk_article_url(link):
        return None
    title = _clean_text(raw_title, MAX_TIME_MK_TITLE_LENGTH)
    if not title:
        return None
    source = ""
    if "|" in title:
        title, source = (part.strip() for part in title.rsplit("|", 1))
        source = _clean_text(source, MAX_TIME_MK_SOURCE_LENGTH)
    if not title:
        return None
    return TimeMkArticle(
        entry_id=_entry_id_from_link(link),
        title=title,
        link=link,
        source=source,
        category=_entry_category(entry),
        published_at=_published_at(entry),
    )


def _entry_id_from_link(link: str) -> EntryId:
    match = TIME_MK_ARTICLE_PATH.search(urlsplit(link).path)
    if match is not None:
        return EntryId(match.group(1))
    return EntryId(link)


def _is_time_mk_article_url(link: str) -> bool:
    try:
        parsed = urlsplit(link)
        port = 443 if parsed.port is None else parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in TIME_MK_HOSTS
        and port == 443
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/c/")
        and not parsed.query
        and not parsed.fragment
    )


def _entry_category(entry: dict[str, object]) -> str | None:
    raw_category = entry.get("category")
    if isinstance(raw_category, str):
        category = _clean_text(raw_category, MAX_TIME_MK_CATEGORY_LENGTH)
        if category:
            return category
    tags = entry.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            term = tag.get("term")
            if isinstance(term, str):
                category = _clean_text(term, MAX_TIME_MK_CATEGORY_LENGTH)
                if category:
                    return category
    return None


def _published_at(entry: dict[str, object]) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not isinstance(parsed, tuple) or len(parsed) < 6:
        return None
    try:
        year, month, day, hour, minute, second = (int(value) for value in parsed[:6])
        return datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=UTC,
        )
    except (TypeError, ValueError):
        return None


def _clean_text(value: str, max_length: int) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in unescape(value)
    )
    return " ".join(printable.split())[:max_length].strip()


def _meta_content(soup: BeautifulSoup, property_name: str) -> str:
    node = soup.select_one(f'meta[property="{property_name}"]')
    if node is None:
        node = soup.select_one(f'meta[name="{property_name}"]')
    if node is None:
        return ""
    content = node.get("content")
    return content if isinstance(content, str) else ""


def _safe_image_url(url: str) -> str | None:
    if not url or len(url) > MAX_TIME_MK_IMAGE_URL_LENGTH:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return url


def _sorted_articles(
    articles: Iterable[TimeMkArticle],
) -> tuple[TimeMkArticle, ...]:
    return tuple(
        sorted(
            articles,
            key=lambda article: (
                article.published_at is None,
                article.published_at or datetime.min.replace(tzinfo=UTC),
                article.entry_id,
            ),
        ),
    )


def _read_content(
    response: requests.Response,
    *,
    remaining_scan_bytes: int,
) -> bytes:
    response_limit = min(MAX_TIME_MK_RESPONSE_BYTES, remaining_scan_bytes)
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_TIME_MK_RESPONSE_BYTES:
            raise FeedFetchError(TIME_MK_LABEL, "ResponseTooLarge")
        if declared_bytes > remaining_scan_bytes:
            raise FeedFetchError(TIME_MK_LABEL, "ScanResponseTooLarge")
    content = bytearray()
    for chunk in response.iter_content(TIME_MK_STREAM_CHUNK_BYTES):
        if len(content) + len(chunk) > response_limit:
            cause = (
                "ResponseTooLarge"
                if remaining_scan_bytes >= MAX_TIME_MK_RESPONSE_BYTES
                else "ScanResponseTooLarge"
            )
            raise FeedFetchError(TIME_MK_LABEL, cause)
        content.extend(chunk)
    return bytes(content)
