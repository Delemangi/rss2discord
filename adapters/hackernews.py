"""Hacker News RSS enrichment."""

import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlsplit

import requests
from pydantic import BaseModel, ConfigDict, ValidationError

from models import EntryData, SourceMetric

logger = logging.getLogger(__name__)
HACKER_NEWS_ITEM_URL: Final = (
    "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
)
HACKER_NEWS_TIMEOUT_SECONDS: Final = 5


class HackerNewsItem(BaseModel):
    """Typed boundary model for a Hacker News API item."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    type: Literal["story", "comment", "job", "poll", "pollopt"]
    by: str | None = None
    time: int | None = None
    text: str | None = None
    url: str | None = None
    score: int | None = None
    descendants: int | None = None
    deleted: bool = False
    dead: bool = False


FetchHackerNewsItem = Callable[[int], HackerNewsItem | None]


def fetch_hacker_news_item(item_id: int) -> HackerNewsItem | None:
    """Fetch one typed Hacker News item."""
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


class HackerNewsAdapter:
    """Enrich RSS entries with official Hacker News item metadata."""

    def __init__(
        self,
        fetch_item: FetchHackerNewsItem = fetch_hacker_news_item,
    ) -> None:
        self._fetch_item = fetch_item

    def adapt(self, entry: Any, data: EntryData) -> EntryData:  # noqa: ANN401
        """Return an enriched entry when a Hacker News item is available."""
        del entry
        item_id = _item_id(data.discussion_url, data.link)
        if item_id is None:
            return data

        item = self._fetch_item(item_id)
        if item is None or item.deleted or item.dead:
            return data

        candidate_link = item.url.strip() if item.url else ""
        link = candidate_link or data.link
        description = data.description
        if item.text:
            description = _clean_hacker_news_text(item.text)
        elif description.strip().casefold() == "comments":
            description = ""

        timestamp = data.timestamp
        if item.time is not None:
            try:
                timestamp = datetime.fromtimestamp(item.time, tz=UTC).isoformat()
            except (OSError, OverflowError, ValueError):
                timestamp = data.timestamp

        metrics: list[SourceMetric] = []
        if item.score is not None:
            metrics.append(SourceMetric(label="Points", value=str(item.score)))
        if item.descendants is not None:
            metrics.append(
                SourceMetric(label="Comments", value=str(item.descendants)),
            )
        domain = _article_domain(link)
        if domain is not None:
            metrics.append(SourceMetric(label="Domain", value=domain))

        return replace(
            data,
            link=link,
            description=description,
            author=item.by or data.author,
            timestamp=timestamp,
            source_metrics=tuple(metrics),
        )


def _item_id(discussion_url: str | None, link: str) -> int | None:
    for candidate in (discussion_url, link):
        if candidate is None:
            continue
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.hostname != "news.ycombinator.com" or parsed.path != "/item":
            continue
        values = parse_qs(parsed.query).get("id", ())
        if len(values) != 1 or not values[0].isdigit():
            continue
        item_id = int(values[0])
        if item_id > 0:
            return item_id
    return None


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
