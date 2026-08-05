from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Final
from zoneinfo import ZoneInfo

from curl_cffi import requests as curl_requests

from rss2discord.transports.pazar3_http import (
    MAX_PAZAR3_ATTEMPT_BYTES,
    Pazar3ScanBudget,
)
from rss2discord.transports.pazar3_scope import Pazar3PageRequest, Pazar3SearchScope

SEARCH_URL: Final = (
    "https://www.pazar3.mk/oglasi/elektronika/delovi-za-kompjuteri-dodatoci/prodazba"
)
FIXED_NOW: Final = datetime(
    2026,
    8,
    5,
    12,
    0,
    tzinfo=ZoneInfo("Europe/Skopje"),
)


def page_request(page: int = 1) -> Pazar3PageRequest:
    return Pazar3SearchScope.from_url(SEARCH_URL).page_request(page)


def scan_budget(
    *,
    bytes_remaining: int = MAX_PAZAR3_ATTEMPT_BYTES,
    redirects_remaining: int = 10,
    expires_at: float = math.inf,
) -> Pazar3ScanBudget:
    return Pazar3ScanBudget(bytes_remaining, redirects_remaining, expires_at)


@dataclass(frozen=True, slots=True)
class Pazar3Card:
    product_id: str = "9086886"
    href: str | None = None
    title: str = "Се продава AOC монитор."
    price: str = "6 000 МКД"
    timestamp: str = "Денес 11:30"
    category: str = "Делови за Компјутери и додатоци"
    city: str = "Скопjе"
    municipality: str = "Аеродром"
    image: str | None = None

    def html(self) -> str:
        href = self.href or (
            "/oglas/elektronika/delovi-za-kompjuteri-dodatoci/prodazba/"
            f"skopje/aerodrom/test-listing/{self.product_id}"
        )
        image = (
            ""
            if self.image is None
            else f'<img data-src="{escape(self.image, quote=True)}">'
        )
        return (
            f'<div class="new row row-listing" data-product-id="{self.product_id}">'
            f'<a class="span2-ad-img-list" href="{escape(href, quote=True)}">'
            f"{image}</a>"
            '<div class="title span-col-title">'
            f'<span class="pull-right ci-text-right">{self.timestamp}</span>'
            f'<a class="link-html nobold" href="{SEARCH_URL}">{self.category}</a>'
            f'<a class="link-html nobold" href="{SEARCH_URL}/skopje">{self.city}</a>'
            f'<a class="link-html nobold" href="{SEARCH_URL}/skopje/aerodrom">'
            f"{self.municipality}</a>"
            '<div class="goodssearch-item-content"><div class="left-side"><h2>'
            f'<a class="Link_vis" href="{escape(href, quote=True)}">{self.title}</a>'
            f'</h2><p class="list-price">{self.price}</p></div></div></div></div>'
        )


def listing_page(
    page: int,
    organic_cards: list[str],
    *,
    total: int,
    top_cards: list[str] | None = None,
    active_sort: str = "DateDesc",
) -> bytes:
    start = 0 if total == 0 else ((page - 1) * 50) + 1
    end = 0 if total == 0 else min(start + len(organic_cards) - 1, total)
    last_page = max(1, (total + 49) // 50)
    links = "".join(
        f'<a class="page-number{" active" if linked_page == page else ""}" '
        f'page-no="{linked_page}" href="'
        f'{Pazar3SearchScope.from_url(SEARCH_URL).catalog_page_request(linked_page).url}">'
        f"{linked_page}</a>"
        for linked_page in range(1, min(last_page, 10) + 1)
    )
    return (
        '<div id="OpensearchListingSearchAdsQuery"><div class="result-content">'
        '<div class="ShimmeredBlockAds form-inline row">'
        f'<div class="top-positioned"><div class="span9">{"".join(top_cards or [])}'
        "</div></div>"
        f'<div class="span9">{"".join(organic_cards)}</div>'
        "</div></div></div>"
        f'<span id="pagination-range"><bdi>{start} - {end}</bdi>'
        f"<bdi>од</bdi><bdi>{total}</bdi><bdi>податоци</bdi></span>"
        f'<div id="paging-control">{links}</div>'
        f'<li class="active"><a data-key="SortingLinks" data-value="{active_sort}">'
        "Најновите први</a></li>"
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    url: str = SEARCH_URL
    chunks: tuple[bytes, ...] | None = None
    interruption: curl_requests.RequestsError | None = None


class RecordingGet:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []
        self.allow_redirects: list[bool] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> StubResponse:
        del timeout_ms
        self.urls.append(url)
        self.headers.append(headers)
        self.allow_redirects.append(allow_redirects)
        response = self.responses.pop(0)
        for chunk in response.chunks or (response.content,):
            content_callback(chunk)
        if response.interruption is not None:
            raise response.interruption
        return response

    def close(self) -> None:
        pass
