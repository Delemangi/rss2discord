from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from types import TracebackType
from typing import Final
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from rss2discord.transports.reklama5_http import (
    MAX_REKLAMA5_ATTEMPT_BYTES,
    Reklama5PageRequest,
    Reklama5ScanBudget,
    Reklama5SearchScope,
)

SEARCH_URL: Final = (
    "https://reklama5.mk/Search?"
    "cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1"
)
FIXED_NOW: Final = datetime(
    2026,
    8,
    1,
    12,
    0,
    tzinfo=ZoneInfo("Europe/Skopje"),
)
FIXTURE_ROOT: Final = Path(__file__).parent / "fixtures" / "reklama5"


def search_scope() -> Reklama5SearchScope:
    return Reklama5SearchScope.from_url(SEARCH_URL)


def fixture_html(name: str) -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


def page_request(url: str = SEARCH_URL) -> Reklama5PageRequest:
    return Reklama5SearchScope.from_url(url).page_request(1)


@dataclass(frozen=True, slots=True)
class Reklama5Card:
    ad_id: str = "9000001"
    href: str | None = None
    title: str | None = "Test listing"
    summary: str = "Test summary"
    price: str = "100 ден."
    location: str = "Скопје"
    category: str = "Тест категорија"
    timestamp: str = "Денес 10:00"
    image: str | None = None
    promotion: str | None = None
    highlighted: bool = False

    def html(self) -> str:
        classes = "ad-top-div OglasResultsHighlighted" if self.highlighted else "ad-top-div"
        href = self.href or f"/AdDetails?ad={self.ad_id}"
        title = "" if self.title is None else f'<a class="SearchAdTitle" href="{href}">{self.title}</a>'
        marker = "" if self.promotion is None else f'<span class="promotedBtn">{self.promotion}</span>'
        image = "" if self.image is None else f'<div class="ad-image" style="background-image: url(\'{self.image}\')"></div>'
        return (
            f'<div class="{classes}">{marker}{title}'
            f'<div class="searchAdDesc">{self.summary}</div>'
            f'<span class="search-ad-price">{self.price}</span>'
            f'<span class="city-span">{self.location}</span>'
            f'<div class="ad-category-div"><small>{self.category}</small></div>'
            f'<div class="ad-date-div-1">{self.timestamp}</div>{image}</div>'
        )


def search_page(
    page: int,
    cards: Sequence[str],
    *,
    page_links: Sequence[int],
    result_count: int,
    active_page: int | None = None,
) -> bytes:
    rows = "".join(cards)
    links = "".join(
        f'<li><a href="{escape(search_scope().page_request(link_page).url, quote=True)}">'
        f"{link_page}</a></li>"
        for link_page in page_links
    )
    active = "" if active_page is None else f'<li class="active">{active_page}</li>'
    return (
        f'<form id="myFrom"><input name="page" value="{page}"></form>'
        f'<div id="sr-holder">{rows}</div>'
        '<span class="float-left">'
        f'<span style="vertical-align: middle">{result_count}</span></span>'
        f'<ul class="pagination">{active}{links}</ul>'
    ).encode()


def replace_form_page_inputs(html: bytes, values: Sequence[str]) -> bytes:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("#myFrom")
    assert form is not None
    for page_input in form.select('input[name="page"]'):
        page_input.decompose()
    for value in values:
        page_input = soup.new_tag("input")
        page_input["name"] = "page"
        page_input["value"] = value
        form.append(page_input)
    return soup.encode()


def replace_active_markers(html: bytes, pages: Sequence[int]) -> bytes:
    soup = BeautifulSoup(html, "html.parser")
    paginator = soup.select_one("ul.pagination")
    assert paginator is not None
    for marker in paginator.select("li.active"):
        marker.decompose()
    for page in pages:
        marker = soup.new_tag("li", attrs={"class": "active"})
        marker.string = str(page)
        paginator.append(marker)
    return soup.encode()


def replace_paginator_hrefs(html: bytes, hrefs: Sequence[str]) -> bytes:
    soup = BeautifulSoup(html, "html.parser")
    paginator = soup.select_one("ul.pagination")
    assert paginator is not None
    for link in paginator.select("a[href]"):
        parent = link.parent
        assert parent is not None
        parent.decompose()
    for href in hrefs:
        item = soup.new_tag("li")
        link = soup.new_tag("a", href=href)
        link.string = "page"
        item.append(link)
        paginator.append(item)
    return soup.encode()


def scan_budget(
    *,
    bytes_remaining: int = MAX_REKLAMA5_ATTEMPT_BYTES,
    redirects_remaining: int = 10,
    expires_at: float = math.inf,
) -> Reklama5ScanBudget:
    return Reklama5ScanBudget(bytes_remaining, redirects_remaining, expires_at)


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    url: str = "https://reklama5.mk/Search"
    chunks: tuple[bytes, ...] | None = None
    interruption: requests.RequestException | None = None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield from self.chunks if self.chunks is not None else (self.content,)
        if self.interruption is not None:
            raise self.interruption

    def __enter__(self) -> StubResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class RecordingGet:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []
        self.timeouts: list[float] = []
        self.allow_redirects: list[bool] = []
        self.stream: list[bool] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        allow_redirects: bool,
        stream: bool,
    ) -> StubResponse:
        self.urls.append(url)
        self.headers.append(headers)
        self.timeouts.append(timeout)
        self.allow_redirects.append(allow_redirects)
        self.stream.append(stream)
        return self.responses.pop(0)
