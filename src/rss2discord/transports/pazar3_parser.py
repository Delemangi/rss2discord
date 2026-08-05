from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from rss2discord.models import EntryId
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.pazar3_models import Pazar3Listing, Pazar3Page
from rss2discord.transports.pazar3_page_validation import (
    normalized_text,
    validate_pazar3_page,
)
from rss2discord.transports.pazar3_scope import (
    PAZAR3_LABEL,
    Pazar3PageRequest,
    is_canonical_pazar3_path,
)

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
_MONTH_PATTERN: Final = re.compile(
    rf"(?P<day>\d{{1,2}}) (?P<month>{'|'.join(_MONTHS)})\. {_TIME_PATTERN}",
)
_MAX_ID_DIGITS: Final = 12
_MAX_YEAR_LOOKBACK: Final = 8


def parse_pazar3_page(
    html: bytes,
    request: Pazar3PageRequest,
    now: datetime,
) -> Pazar3Page:
    if now.tzinfo is None or now.utcoffset() is None:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidClock")
    local_now = now.astimezone(SKOPJE)
    soup = BeautifulSoup(html, "html.parser")
    roots = soup.select("#OpensearchListingSearchAdsQuery")
    if len(roots) != 1:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
    result_lists = roots[0].select(".ShimmeredBlockAds.form-inline.row")
    if len(result_lists) != 1:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
    promoted_blocks = result_lists[0].select(".top-positioned")
    if len(promoted_blocks) > 1 or (
        promoted_blocks and len(promoted_blocks[0].select("[data-product-id]")) > 3
    ):
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPromotedListings")
    rows = [
        row
        for row in result_lists[0].select("[data-product-id]")
        if row.find_parent(class_="top-positioned") is None
    ]
    evidence = validate_pazar3_page(soup, request, len(rows))
    listings: list[Pazar3Listing] = []
    organic_ids: set[EntryId] = set()
    for row in rows:
        identity = _listing_identity(row, request)
        link, entry_id, canonical_url = identity
        organic_ids.add(entry_id)
        title = normalized_text(link)
        activity_at = _parse_activity(
            normalized_text(row.select_one(".pull-right.ci-text-right")),
            local_now,
        )
        if not title or activity_at is None:
            continue
        labels = [normalized_text(node) for node in row.select("a.link-html.nobold")]
        category = labels[0] if labels else ""
        location_labels = labels[-2:] if len(labels) >= 3 else labels[1:]
        listings.append(
            Pazar3Listing(
                entry_id=entry_id,
                url=canonical_url,
                title=title,
                price=normalized_text(row.select_one(".list-price")),
                location=", ".join(location_labels),
                category=category,
                activity_at=activity_at,
                image_url=_image_url(row),
            ),
        )
    return Pazar3Page(
        listings=tuple(listings),
        organic_ids=frozenset(organic_ids),
        organic_row_count=len(rows),
        result_count=evidence.result_count,
        terminal=evidence.terminal,
    )


def _listing_identity(
    row: Tag,
    request: Pazar3PageRequest,
) -> tuple[Tag, EntryId, str]:
    raw_id_value = row.get("data-product-id")
    raw_id = raw_id_value if isinstance(raw_id_value, str) else None
    links = row.select("a.Link_vis[href]")
    if (
        raw_id is None
        or raw_id != raw_id.strip()
        or not raw_id.isascii()
        or not raw_id.isdecimal()
        or len(raw_id) > _MAX_ID_DIGITS
        or raw_id != str(int(raw_id))
        or len(links) != 1
    ):
        raise FeedFetchError(PAZAR3_LABEL, "InvalidListingIdentity")
    link = links[0]
    href = _attribute(link, "href")
    if href is None:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidListingIdentity")
    try:
        parsed = urlsplit(urljoin(f"https://{request.scope.host}/", href))
        port = 443 if parsed.port is None else parsed.port
    except ValueError:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidListingIdentity") from None
    path_id = parsed.path.rstrip("/").rsplit("/", maxsplit=1)[-1]
    if (
        parsed.scheme != "https"
        or parsed.hostname != request.scope.host
        or port != request.scope.port
        or parsed.username is not None
        or parsed.password is not None
        or not is_canonical_pazar3_path(parsed.path, "/oglas/")
        or parsed.query
        or parsed.fragment
        or path_id != raw_id
    ):
        raise FeedFetchError(PAZAR3_LABEL, "InvalidListingIdentity")
    return (
        link,
        EntryId(raw_id),
        urlunsplit(("https", request.scope.host, parsed.path, "", "")),
    )


def _parse_activity(value: str, local_now: datetime) -> datetime | None:
    try:
        if match := _TODAY_PATTERN.fullmatch(value):
            return _localize(local_now.date(), match)
        if match := _YESTERDAY_PATTERN.fullmatch(value):
            return _localize(local_now.date() - timedelta(days=1), match)
        match = _MONTH_PATTERN.fullmatch(value)
        if match is None:
            return None
        month = _MONTHS[match.group("month")]
        day = int(match.group("day"))
        for year in range(local_now.year, local_now.year - _MAX_YEAR_LOOKBACK - 1, -1):
            try:
                wall_date = date(year, month, day)
            except ValueError:
                continue
            candidate = _localize(wall_date, match)
            if candidate is not None and candidate.astimezone(
                UTC,
            ) <= local_now.astimezone(UTC):
                return candidate
    except ValueError:
        return None
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


def _image_url(row: Tag) -> str | None:
    image = row.select_one("img[data-src]")
    raw_url = _attribute(image, "data-src")
    if raw_url is None:
        return None
    try:
        parsed = urlsplit(raw_url)
        port = 443 if parsed.port is None else parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "media.pazar3.mk"
        or port != 443
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return raw_url


def _attribute(node: Tag | None, name: str) -> str | None:
    if node is None:
        return None
    value = node.get(name)
    return value.strip() if isinstance(value, str) else None
