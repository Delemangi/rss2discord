"""Bounded Fakulteti.mk JSON API traversal."""

import json
import logging
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from html import unescape
from typing import Any, Final
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

import requests

from rss2discord.models import EntryId
from rss2discord.providers.fakulteti_mk.bounds import (
    FAKULTETI_MK_ALL_CATEGORIES,
    FAKULTETI_MK_API_PATH,
    FAKULTETI_MK_HOSTS,
    FAKULTETI_MK_LABEL,
    FAKULTETI_MK_ORIGIN,
    FAKULTETI_MK_PAGE_LIMIT,
    FAKULTETI_MK_REQUEST_SECONDS,
    FAKULTETI_MK_STREAM_CHUNK_BYTES,
    FAKULTETI_MK_USER_AGENT,
    MAX_FAKULTETI_MK_CATEGORIES,
    MAX_FAKULTETI_MK_CATEGORY_LENGTH,
    MAX_FAKULTETI_MK_DESCRIPTION_LENGTH,
    MAX_FAKULTETI_MK_IMAGE_URL_LENGTH,
    MAX_FAKULTETI_MK_POSTS,
    MAX_FAKULTETI_MK_RESPONSE_BYTES,
    MAX_FAKULTETI_MK_SCAN_BYTES,
    MAX_FAKULTETI_MK_SCAN_SECONDS,
    MAX_FAKULTETI_MK_TITLE_LENGTH,
)
from rss2discord.providers.fakulteti_mk.models import FakultetiMkPost
from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.url_normalization import normalize_remote_media_url

logger = logging.getLogger(__name__)

FAKULTETI_MK_TIME_ZONE: Final = ZoneInfo("Europe/Skopje")
FAKULTETI_MK_DATE_FORMAT: Final = "%d.%m.%Y"
FAKULTETI_MK_TERMINAL_CAUSES: Final = frozenset(
    {"ScanTimeout", "ScanResponseTooLarge"},
)


class FakultetiMkCatalogClient:
    """Fetch one bounded Fakulteti.mk category subset through its JSON API."""

    def fetch_posts(self, url: str) -> tuple[FakultetiMkPost, ...]:
        categories = normalize_fakulteti_mk_url(url)
        deadline = time.monotonic() + MAX_FAKULTETI_MK_SCAN_SECONDS
        remaining_bytes = MAX_FAKULTETI_MK_SCAN_BYTES
        posts: dict[str, FakultetiMkPost] = {}
        successes = 0

        if not categories:
            items, used = self._fetch_items(
                {
                    "command": "GetLatestPosts",
                    "offset": "0",
                    "limit": str(FAKULTETI_MK_PAGE_LIMIT),
                },
                deadline,
                remaining_bytes,
            )
            successes = 1
            remaining_bytes -= used
            self._add_posts(posts, items, None)
        else:
            for category in categories:
                try:
                    items, used = self._fetch_items(
                        {
                            "command": "GetTaxonomyPosts",
                            "taxonomy": "category",
                            "term": category,
                            "offset": "0",
                            "limit": str(FAKULTETI_MK_PAGE_LIMIT),
                            "includeTop": "false",
                        },
                        deadline,
                        remaining_bytes,
                    )
                except FeedFetchError as error:
                    if error.cause_type in FAKULTETI_MK_TERMINAL_CAUSES:
                        raise
                    logger.warning(
                        "Fakulteti.mk category fetch failed: category=%s cause=%s; "
                        "continuing best-effort scan",
                        category,
                        error.cause_type,
                    )
                    continue
                successes += 1
                remaining_bytes -= used
                self._add_posts(posts, items, category)

        if successes == 0:
            raise FeedFetchError(FAKULTETI_MK_LABEL, "EmptyResponse")
        return _sorted_posts(posts.values())

    def _fetch_items(
        self,
        params: dict[str, str],
        deadline: float,
        remaining_bytes: int,
    ) -> tuple[list[Any], int]:
        if time.monotonic() >= deadline:
            raise FeedFetchError(FAKULTETI_MK_LABEL, "ScanTimeout")
        try:
            with requests.get(
                f"{FAKULTETI_MK_ORIGIN}{FAKULTETI_MK_API_PATH}",
                params=params,
                headers={"User-Agent": FAKULTETI_MK_USER_AGENT},
                timeout=FAKULTETI_MK_REQUEST_SECONDS,
                stream=True,
            ) as response:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    status_code = response.status_code
                    raise FeedFetchError(
                        FAKULTETI_MK_LABEL,
                        "HTTPError",
                        status_code=status_code,
                        retryable=status_code == 429 or 500 <= status_code < 600,
                        retry_after=parse_retry_after(
                            response.headers.get("Retry-After"),
                        ),
                    ) from None
                content = _read_content(response, remaining_bytes)
        except FeedFetchError:
            raise
        except (requests.ConnectionError, requests.Timeout) as error:
            raise FeedFetchError(
                FAKULTETI_MK_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(
                FAKULTETI_MK_LABEL,
                type(error).__name__,
            ) from None

        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            raise FeedFetchError(FAKULTETI_MK_LABEL, "InvalidJson") from None
        if not isinstance(payload, list):
            raise FeedFetchError(FAKULTETI_MK_LABEL, "InvalidJson")
        return payload, len(content)

    def _add_posts(
        self,
        posts: dict[str, FakultetiMkPost],
        items: list[Any],
        category_hint: str | None,
    ) -> None:
        for item in items[:FAKULTETI_MK_PAGE_LIMIT]:
            post = _post_from_item(item, category_hint)
            if post is not None:
                posts.setdefault(post.link, post)


def normalize_fakulteti_mk_url(url: str) -> tuple[str, ...]:
    """Map one Fakulteti.mk URL to the ordered categories it should fetch."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(FAKULTETI_MK_LABEL, "InvalidUrl") from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or hostname.casefold() not in FAKULTETI_MK_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(FAKULTETI_MK_LABEL, "InvalidUrl")
    path = parsed.path
    if path in {"", "/"}:
        return ()
    if not path.startswith("/category/"):
        raise FeedFetchError(FAKULTETI_MK_LABEL, "InvalidUrl")
    raw = unquote(path.removeprefix("/category/")).strip("/")
    categories = tuple(
        dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()),
    )
    if not categories or len(categories) > MAX_FAKULTETI_MK_CATEGORIES:
        raise FeedFetchError(FAKULTETI_MK_LABEL, "InvalidUrl")
    if any(category not in FAKULTETI_MK_ALL_CATEGORIES for category in categories):
        raise FeedFetchError(FAKULTETI_MK_LABEL, "InvalidUrl")
    return categories


def _read_content(response: requests.Response, remaining_bytes: int) -> bytes:
    if remaining_bytes <= 0:
        raise FeedFetchError(FAKULTETI_MK_LABEL, "ScanResponseTooLarge")
    limit = min(MAX_FAKULTETI_MK_RESPONSE_BYTES, remaining_bytes)
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > limit:
            raise FeedFetchError(FAKULTETI_MK_LABEL, "ScanResponseTooLarge")
    content = bytearray()
    for chunk in response.iter_content(chunk_size=FAKULTETI_MK_STREAM_CHUNK_BYTES):
        if len(content) + len(chunk) > limit:
            raise FeedFetchError(FAKULTETI_MK_LABEL, "ScanResponseTooLarge")
        content.extend(chunk)
    return bytes(content)


def _post_from_item(
    item: Any,  # noqa: ANN401
    category_hint: str | None,
) -> FakultetiMkPost | None:
    if not isinstance(item, Mapping):
        return None
    link = _safe_post_url(item.get("url"))
    title = _clean_text(item.get("title"), MAX_FAKULTETI_MK_TITLE_LENGTH)
    if link is None or not title:
        return None
    category = _category_name(item.get("category")) or category_hint
    comments, shares = _facebook_counts(item.get("facebook"))
    return FakultetiMkPost(
        entry_id=EntryId(link),
        title=title,
        link=link,
        description=_clean_text(
            item.get("description"),
            MAX_FAKULTETI_MK_DESCRIPTION_LENGTH,
        ),
        image_url=_safe_image_url(item.get("imageUrl")),
        published_at=_parse_date(item.get("publicationDate")),
        category=category,
        comments=comments,
        shares=shares,
    )


def _safe_post_url(value: Any) -> str | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in FAKULTETI_MK_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return normalized


def _safe_image_url(value: Any) -> str | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    normalized = normalize_remote_media_url(value)
    if normalized is None or len(normalized) > MAX_FAKULTETI_MK_IMAGE_URL_LENGTH:
        return None
    return normalized


def _clean_text(value: Any, max_length: int) -> str:  # noqa: ANN401
    if not isinstance(value, str):
        return ""
    printable = "".join(
        character if character.isprintable() else " " for character in unescape(value)
    )
    return " ".join(printable.split())[:max_length].strip()


def _parse_date(value: Any) -> datetime | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(  # noqa: DTZ007
            value.strip(),
            FAKULTETI_MK_DATE_FORMAT,
        )
    except ValueError:
        return None
    return parsed.replace(tzinfo=FAKULTETI_MK_TIME_ZONE).astimezone(UTC)


def _category_name(value: Any) -> str | None:  # noqa: ANN401
    if not isinstance(value, Mapping):
        return None
    name = _clean_text(value.get("name"), MAX_FAKULTETI_MK_CATEGORY_LENGTH)
    return name or None


def _facebook_counts(value: Any) -> tuple[int, int]:  # noqa: ANN401
    if not isinstance(value, Mapping):
        return (0, 0)

    def count(key: str) -> int:
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return 0
        return min(raw, 1_000_000)

    return (count("comments"), count("shares"))


def _sorted_posts(
    posts: Iterable[FakultetiMkPost],
) -> tuple[FakultetiMkPost, ...]:
    ordered = sorted(
        posts,
        key=lambda post: (
            post.published_at is None,
            post.published_at or datetime.min.replace(tzinfo=UTC),
            post.link,
        ),
    )
    return tuple(ordered[:MAX_FAKULTETI_MK_POSTS])
