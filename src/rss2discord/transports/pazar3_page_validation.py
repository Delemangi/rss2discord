from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.pazar3_scope import PAZAR3_LABEL, Pazar3PageRequest

_RANGE_PATTERN: Final = re.compile(r"(?P<start>\d+)\s*-\s*(?P<end>\d+)")
_MAX_DECIMAL_DIGITS: Final = 10
_PAGE_SIZE: Final = 50


@dataclass(frozen=True, slots=True)
class Pazar3PageEvidence:
    result_count: int
    terminal: bool


def validate_pazar3_page(
    soup: BeautifulSoup,
    request: Pazar3PageRequest,
    organic_row_count: int,
) -> Pazar3PageEvidence:
    roots = soup.select("#OpensearchListingSearchAdsQuery")
    ranges = soup.select("#pagination-range")
    pagers = soup.select("#paging-control")
    newest = soup.select(
        'li.active > a[data-key="SortingLinks"][data-value="DateDesc"]',
    )
    if len(roots) != 1 or len(ranges) != 1 or len(pagers) != 1 or len(newest) != 1:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")

    range_parts = ranges[0].select("bdi")
    if len(range_parts) != 4:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
    match = _RANGE_PATTERN.fullmatch(normalized_text(range_parts[0]))
    result_count = _non_negative_decimal(normalized_text(range_parts[2]))
    if match is None or result_count is None:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
    range_start = _non_negative_decimal(match.group("start"))
    range_end = _non_negative_decimal(match.group("end"))
    if range_start is None or range_end is None:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")

    expected_start = 0 if result_count == 0 else ((request.page - 1) * _PAGE_SIZE) + 1
    expected_rows = 0 if result_count == 0 else range_end - range_start + 1
    if (
        range_start != expected_start
        or range_end > result_count
        or expected_rows != organic_row_count
    ):
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
    terminal = result_count in {0, range_end}
    if organic_row_count > _PAGE_SIZE or (
        not terminal and organic_row_count != _PAGE_SIZE
    ):
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")

    active_pages = pagers[0].select("a.page-number.active")
    if len(active_pages) != 1:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
    if _positive_decimal(_attribute(active_pages[0], "page-no")) != request.page:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
    target_pages = [
        _validate_paginator_link(link, request)
        for link in pagers[0].select("a")
        if "disabled" not in _classes(link)
    ]
    last_page = max(1, (result_count + _PAGE_SIZE - 1) // _PAGE_SIZE)
    if any(page > last_page for page in target_pages) or (
        terminal and any(page > request.page for page in target_pages)
    ):
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPaginator")

    return Pazar3PageEvidence(
        result_count=result_count,
        terminal=terminal,
    )


def normalized_text(node: BeautifulSoup | Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _validate_paginator_link(link: Tag, request: Pazar3PageRequest) -> int:
    href = _attribute(link, "href")
    page_number = _positive_decimal(_attribute(link, "page-no"))
    if href is None or page_number is None or "#" in href:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPaginator")
    parsed = urlsplit(href)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    page_values = [value for key, value in query if key.casefold() == "page"]
    if not page_values and page_number == 1:
        current_query = [*query, ("Page", str(request.page))]
    elif len(page_values) == 1 and _positive_decimal(page_values[0]) == page_number:
        current_query = [
            (key, str(request.page) if key.casefold() == "page" else value)
            for key, value in query
        ]
    else:
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPaginator")
    current_url = urlunsplit((*parsed[:3], urlencode(current_query), parsed.fragment))
    if not request.scope.accepts_redirect(request, current_url):
        raise FeedFetchError(PAZAR3_LABEL, "InvalidPaginator")
    return page_number


def _classes(node: Tag) -> frozenset[str]:
    value = node.get("class")
    return (
        frozenset(item for item in value if isinstance(item, str))
        if value
        else frozenset()
    )


def _attribute(node: Tag, name: str) -> str | None:
    value = node.get(name)
    return value.strip() if isinstance(value, str) else None


def _non_negative_decimal(value: str | None) -> int | None:
    if value is None or len(value) > _MAX_DECIMAL_DIGITS or not value.isdecimal():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _positive_decimal(value: str | None) -> int | None:
    parsed = _non_negative_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None
