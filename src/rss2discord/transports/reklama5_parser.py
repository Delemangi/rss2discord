from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from urllib.parse import parse_qsl, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from rss2discord.models import EntryId
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.reklama5_page_validation import (
    normalized_text,
    validate_reklama5_page,
)
from rss2discord.transports.reklama5_scope import REKLAMA5_LABEL, Reklama5PageRequest

SKOPJE: Final = ZoneInfo("Europe/Skopje")

_MONTHS: Final = {
    "јан": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мај": 5,
    "јун": 6,
    "јул": 7,
    "авг": 8,
    "сеп": 9,
    "окт": 10,
    "ное": 11,
    "дек": 12,
}
_TIME_PATTERN: Final = r"(?P<hour>\d{2}):(?P<minute>\d{2})"
_TODAY_PATTERN: Final = re.compile(rf"Денес {_TIME_PATTERN}", re.ASCII)
_YESTERDAY_PATTERN: Final = re.compile(rf"Вчера {_TIME_PATTERN}", re.ASCII)
_DATE_PATTERN: Final = re.compile(
    rf"(?P<day>\d{{2}})/(?P<month>\d{{2}})/(?P<year>\d{{4}}) {_TIME_PATTERN}",
    re.ASCII,
)
_MONTH_PATTERN: Final = re.compile(
    rf"(?P<day>\d{{1,2}}) (?P<month>{'|'.join(_MONTHS)}) {_TIME_PATTERN}",
)
_IMAGE_PATTERN: Final = re.compile(
    r"background-image\s*:\s*url\(\s*(['\"]?)(?P<url>[^)'\"]+)\1\s*\)",
    re.IGNORECASE,
)
_SUMMARY_LENGTH: Final = 2_000


@dataclass(frozen=True, slots=True)
class Reklama5Listing:
    entry_id: EntryId
    url: str
    title: str
    summary: str
    price: str
    location: str
    category: str
    activity_at: datetime
    image_url: str | None


@dataclass(frozen=True, slots=True)
class Reklama5Page:
    listings: tuple[Reklama5Listing, ...]
    organic_ids: frozenset[EntryId]
    terminal: bool


def parse_reklama5_page(
    html: bytes,
    request: Reklama5PageRequest,
    now: datetime,
) -> Reklama5Page:
    if now.tzinfo is None or now.utcoffset() is None:
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidClock")
    local_now = now.astimezone(SKOPJE)
    soup = BeautifulSoup(html, "html.parser")
    evidence = validate_reklama5_page(soup, request)
    listings: list[Reklama5Listing] = []
    organic_ids: set[EntryId] = set()
    for card in soup.select("#sr-holder > .ad-top-div"):
        promotion = card.select_one(".promotedBtn")
        if promotion is not None:
            if normalized_text(promotion) == "Промовирано":
                continue
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidPromotionMarker")

        identity = _listing_identity(card, request)
        if identity is None:
            continue
        link, entry_id, canonical_url = identity
        organic_ids.add(entry_id)
        title = normalized_text(link)
        activity_at = _parse_activity(
            normalized_text(card.select_one(".ad-date-div-1")),
            local_now,
        )
        if not title or activity_at is None:
            continue
        summary = normalized_text(card.select_one(".searchAdDesc"))
        if len(summary) > _SUMMARY_LENGTH:
            summary = f"{summary[: _SUMMARY_LENGTH - 3]}..."
        listings.append(
            Reklama5Listing(
                entry_id=entry_id,
                url=canonical_url,
                title=title,
                summary=summary,
                price=normalized_text(card.select_one(".search-ad-price")),
                location=normalized_text(card.select_one(".city-span")),
                category=normalized_text(card.select_one(".ad-category-div small")),
                activity_at=activity_at,
                image_url=_image_url(card, request),
            ),
        )
    frozen_ids = frozenset(organic_ids)
    has_paginator_links = bool(evidence.paginator_pages)
    if evidence.result_count == 0:
        if frozen_ids or has_paginator_links:
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")
        terminal = True
    else:
        terminal = bool(frozen_ids) and not any(
            page > request.page for page in evidence.paginator_pages
        )
    return Reklama5Page(tuple(listings), frozen_ids, terminal)


def _listing_identity(
    card: Tag,
    request: Reklama5PageRequest,
) -> tuple[Tag, EntryId, str] | None:
    link = card.select_one(".SearchAdTitle[href]")
    href = _attribute(link, "href")
    if link is None or href is None:
        return None
    origin = f"https://{request.scope.host}"
    try:
        parsed = urlsplit(urljoin(f"{origin}/", href))
        port = 443 if parsed.port is None else parsed.port
    except ValueError:
        return None
    ad_values = [value for key, value in parse_qsl(parsed.query) if key == "ad"]
    if (
        parsed.scheme != "https"
        or parsed.hostname != request.scope.host
        or port != request.scope.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/AdDetails"
        or len(ad_values) != 1
        or not ad_values[0].isdecimal()
    ):
        return None
    entry_id = EntryId(ad_values[0])
    return link, entry_id, f"{origin}/AdDetails?ad={entry_id}"


def _parse_activity(value: str, local_now: datetime) -> datetime | None:
    try:
        if match := _TODAY_PATTERN.fullmatch(value):
            wall_date = local_now.date()
        elif match := _YESTERDAY_PATTERN.fullmatch(value):
            wall_date = local_now.date() - timedelta(days=1)
        elif match := _DATE_PATTERN.fullmatch(value):
            wall_date = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        elif match := _MONTH_PATTERN.fullmatch(value):
            wall_date = date(
                local_now.year,
                _MONTHS[match.group("month")],
                int(match.group("day")),
            )
            candidate = _localize(wall_date, match)
            if candidate is not None and candidate > local_now:
                wall_date = wall_date.replace(year=wall_date.year - 1)
        else:
            return None
        return _localize(wall_date, match)
    except ValueError:
        return None


def _localize(wall_date: date, match: re.Match[str]) -> datetime | None:
    wall = datetime.combine(
        wall_date,
        time(int(match.group("hour")), int(match.group("minute"))),
    )
    localized = wall.replace(tzinfo=SKOPJE, fold=0)
    round_trip = localized.astimezone(UTC).astimezone(SKOPJE)
    if round_trip.replace(tzinfo=None) != wall or round_trip.fold != localized.fold:
        return None
    return localized


def _image_url(card: Tag, request: Reklama5PageRequest) -> str | None:
    style = _attribute(card.select_one(".ad-image"), "style")
    if style is None or (match := _IMAGE_PATTERN.search(style)) is None:
        return None
    raw_url = match.group("url").strip()
    candidate = (
        urljoin(f"https://{request.scope.host}/", raw_url)
        if raw_url.startswith("//")
        else raw_url
    )
    try:
        parsed = urlsplit(candidate)
        host, _ = parsed.hostname, parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return candidate


def _attribute(node: Tag | None, name: str) -> str | None:
    if node is None:
        return None
    value = node.get(name)
    if not isinstance(value, str):
        return None
    return value.strip()
