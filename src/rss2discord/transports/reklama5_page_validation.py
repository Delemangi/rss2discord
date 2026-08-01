from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.reklama5_scope import REKLAMA5_LABEL, Reklama5PageRequest

REKLAMA5_APPLICATION_ERROR_TEXT: Final = (
    "Настана грешка. Оваа грешка е испратена до нашиот технички оддел."
)


@dataclass(frozen=True, slots=True)
class Reklama5PageEvidence:
    result_count: int
    paginator_pages: tuple[int, ...]


def validate_reklama5_page(
    soup: BeautifulSoup,
    request: Reklama5PageRequest,
) -> Reklama5PageEvidence:
    if REKLAMA5_APPLICATION_ERROR_TEXT in normalized_text(soup):
        raise FeedFetchError(REKLAMA5_LABEL, "ApplicationError")

    forms = soup.select("#myFrom")
    result_holders = soup.select("#sr-holder")
    paginators = soup.select("ul.pagination")
    if len(forms) != 1 or len(result_holders) != 1 or len(paginators) != 1:
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")

    page_inputs = forms[0].select('input[name="page"]')
    if len(page_inputs) != 1:
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")
    page_value = _attribute(page_inputs[0], "value")
    if page_value is None or not _is_positive_decimal(page_value):
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")
    if int(page_value) != request.page:
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")

    count_nodes = soup.select('span.float-left > span[style*="vertical-align"]')
    if len(count_nodes) != 1:
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")
    count_value = normalized_text(count_nodes[0])
    if not count_value.isdecimal():
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")

    paginator = paginators[0]
    paginator_pages = tuple(
        _validate_paginator_link(_attribute(link, "href"), request)
        for link in paginator.select("a[href]")
    )
    if paginator_pages:
        active_markers = paginator.select("li.active")
        if len(active_markers) != 1:
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")
        active_value = normalized_text(active_markers[0])
        if not _is_positive_decimal(active_value) or int(active_value) != request.page:
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")

    return Reklama5PageEvidence(
        result_count=int(count_value),
        paginator_pages=paginator_pages,
    )


def normalized_text(node: BeautifulSoup | Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _validate_paginator_link(
    href: str | None,
    request: Reklama5PageRequest,
) -> int:
    if href is None:
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPaginator")
    absolute_url = urljoin(request.url, href)
    parsed = urlsplit(absolute_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    page_values = [value for key, value in query if key.casefold() == "page"]
    if len(page_values) != 1 or not _is_positive_decimal(page_values[0]):
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPaginator")
    current_query = [
        (key, str(request.page) if key.casefold() == "page" else value)
        for key, value in query
    ]
    current_page_url = urlunsplit((*parsed[:3], urlencode(current_query), parsed.fragment))
    if not request.scope.accepts_redirect(request, current_page_url):
        raise FeedFetchError(REKLAMA5_LABEL, "InvalidPaginator")
    return int(page_values[0])


def _attribute(node: Tag, name: str) -> str | None:
    value = node.get(name)
    if not isinstance(value, str):
        return None
    return value.strip()


def _is_positive_decimal(value: str | None) -> bool:
    return value is not None and value.isdecimal() and int(value) > 0
