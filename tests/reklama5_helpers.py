from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final

import requests

from rss2discord.transports.reklama5_http import (
    MAX_REKLAMA5_ATTEMPT_BYTES,
    Reklama5ScanBudget,
    Reklama5SearchScope,
)

SEARCH_URL: Final = (
    "https://reklama5.mk/Search?"
    "cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1"
)


def search_scope() -> Reklama5SearchScope:
    return Reklama5SearchScope.from_url(SEARCH_URL)


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
