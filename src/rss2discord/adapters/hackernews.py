import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from html import unescape
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlsplit

import requests
from pydantic import BaseModel, ConfigDict, ValidationError

from rss2discord.models import EntryData, SourceMetric
from rss2discord.url_normalization import normalize_http_url

logger = logging.getLogger(__name__)
HACKER_NEWS_ITEM_URL: Final = (
    "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
)
HACKER_NEWS_TOPSTORIES_URL: Final = (
    "https://hacker-news.firebaseio.com/v0/topstories.json"
)
HACKER_NEWS_TIMEOUT_SECONDS: Final = 5
HACKER_NEWS_TOPSTORIES_TTL_SECONDS: Final = 300
HACKER_NEWS_MAX_TOPSTORIES: Final = 500
MAX_HACKER_NEWS_COMMENT_LENGTH: Final = 400


class HackerNewsItem(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    type: Literal["story", "comment", "job", "poll", "pollopt"]
    by: str | None = None
    time: int | None = None
    text: str | None = None
    url: str | None = None
    score: int | None = None
    descendants: int | None = None
    kids: tuple[int, ...] = ()
    deleted: bool = False
    dead: bool = False


FetchHackerNewsItem = Callable[[int], HackerNewsItem | None]
FetchHackerNewsTopStories = Callable[[], tuple[int, ...] | None]


def fetch_hacker_news_item(item_id: int) -> HackerNewsItem | None:
    try:
        response = requests.get(
            HACKER_NEWS_ITEM_URL.format(item_id=item_id),
            timeout=HACKER_NEWS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return HackerNewsItem.model_validate(response.json())
    except (requests.RequestException, ValidationError) as error:
        logger.warning(
            "Could not enrich Hacker News item %d (%s)",
            item_id,
            type(error).__name__,
        )
        return None


def fetch_hacker_news_topstories() -> tuple[int, ...] | None:
    try:
        response = requests.get(
            HACKER_NEWS_TOPSTORIES_URL,
            timeout=HACKER_NEWS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        logger.warning(
            "Could not enrich Hacker News front-page rank (%s)",
            type(error).__name__,
        )
        return None
    if not isinstance(payload, list) or len(payload) > HACKER_NEWS_MAX_TOPSTORIES:
        return None
    item_ids: list[int] = []
    for value in payload:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
        item_ids.append(value)
    return tuple(item_ids)


class HackerNewsAdapter:
    def __init__(
        self,
        fetch_item: FetchHackerNewsItem = fetch_hacker_news_item,
        fetch_topstories: FetchHackerNewsTopStories | None = None,
    ) -> None:
        self._fetch_item = fetch_item
        self._fetch_topstories = fetch_topstories
        self._topstories: tuple[int, ...] | None = None
        self._topstories_at: float | None = None

    def adapt(self, entry: Any, data: EntryData) -> EntryData:  # noqa: ANN401
        del entry
        item_id = _item_id(data.discussion_url, data.link)
        if item_id is None:
            return data

        item = self._fetch_item(item_id)
        if item is None or item.deleted or item.dead:
            return data

        candidate_link = normalize_http_url(item.url) if item.url else None
        link = candidate_link or data.link
        description = data.description
        if item.text:
            description = _clean_hacker_news_text(item.text)
        elif description.strip().casefold() == "comments":
            description = ""
        if not description:
            top_comment = self._top_comment(item)
            if top_comment is not None and top_comment.text:
                comment_text = _clean_hacker_news_text(top_comment.text)
                if comment_text:
                    author = f" — {top_comment.by}" if top_comment.by else ""
                    description = _truncate(
                        f"Top comment: {comment_text}{author}",
                        MAX_HACKER_NEWS_COMMENT_LENGTH,
                    )

        timestamp = data.timestamp
        if item.time is not None:
            try:
                timestamp = datetime.fromtimestamp(item.time, tz=UTC).isoformat()
            except (OSError, OverflowError, ValueError):
                timestamp = data.timestamp

        metrics: list[SourceMetric] = []
        rank = self._rank_for(item.id)
        if rank is not None:
            metrics.append(SourceMetric(label="Rank", value=f"#{rank}"))
        if item.score is not None:
            metrics.append(SourceMetric(label="Points", value=str(item.score)))
        if item.descendants is not None:
            metrics.append(
                SourceMetric(label="Comments", value=str(item.descendants)),
            )
        domain = _article_domain(link)
        if domain is not None:
            metrics.append(SourceMetric(label="Domain", value=domain))
        if item.type != "story":
            metrics.append(SourceMetric(label="Type", value=item.type.title()))

        return EntryData(
            title=data.title,
            link=link,
            description=description,
            author=item.by or data.author,
            timestamp=timestamp,
            discussion_url=data.discussion_url,
            image_url=data.image_url,
            categories=data.categories,
            source_metrics=tuple(metrics),
        )

    def _rank_for(self, item_id: int) -> int | None:
        if self._fetch_topstories is None:
            return None
        now = time.monotonic()
        if (
            self._topstories_at is None
            or now - self._topstories_at >= HACKER_NEWS_TOPSTORIES_TTL_SECONDS
        ):
            self._topstories = self._fetch_topstories()
            self._topstories_at = now
        if not self._topstories:
            return None
        try:
            return self._topstories.index(item_id) + 1
        except ValueError:
            return None

    def _top_comment(self, item: HackerNewsItem) -> HackerNewsItem | None:
        if not item.kids:
            return None
        comment = self._fetch_item(item.kids[0])
        if comment is None or comment.deleted or comment.dead:
            return None
        return comment


def _item_id(discussion_url: str | None, link: str) -> int | None:
    for candidate in (discussion_url, link):
        if candidate is None:
            continue
        item_id = _item_id_from_url(candidate)
        if item_id is not None:
            return item_id
    return None


def _item_id_from_url(candidate: str) -> int | None:
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.hostname != "news.ycombinator.com" or parsed.path != "/item":
        return None
    values = parse_qs(parsed.query).get("id", ())
    if len(values) != 1:
        return None
    raw_item_id = values[0]
    if len(raw_item_id) > 20 or not raw_item_id.isascii() or not raw_item_id.isdigit():
        return None
    try:
        item_id = int(raw_item_id)
    except ValueError:
        return None
    return item_id if item_id > 0 else None


def _article_domain(link: str) -> str | None:
    try:
        hostname = urlsplit(link).hostname
    except ValueError:
        return None
    if hostname is None or hostname == "news.ycombinator.com":
        return None
    return hostname.removeprefix("www.")


def _clean_hacker_news_text(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."
