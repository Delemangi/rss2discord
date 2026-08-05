from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Final
from urllib.parse import (
    SplitResult,
    parse_qsl,
    unquote,
    urlencode,
    urlsplit,
    urlunsplit,
)

from rss2discord.transports.base import FeedFetchError

PAZAR3_LABEL: Final = "Pazar3"
MAX_PAZAR3_CATALOG_PAGES: Final = 10
_MAX_DISCOVERY_PAGE: Final = 3
_ALLOWED_HOSTS: Final = frozenset({"pazar3.mk", "www.pazar3.mk"})
_OWNED_QUERY_KEYS: Final = frozenset({"page"})
_INVALID_PERCENT_ESCAPE: Final = re.compile(r"%(?![0-9a-fA-F]{2})")

type _ParsedListingUrl = tuple[SplitResult, str, int]


@dataclass(frozen=True, slots=True)
class Pazar3SearchScope:
    scheme: str
    host: str
    port: int
    configured_path: str
    caller_query: tuple[tuple[str, str], ...]

    @classmethod
    def from_url(cls, url: str) -> Pazar3SearchScope:
        trusted_url = _parse_listing_url(url)
        if trusted_url is None:
            raise FeedFetchError(PAZAR3_LABEL, "InvalidUrl")
        parsed, host, port = trusted_url
        return cls(
            scheme=parsed.scheme,
            host=host,
            port=port,
            configured_path=parsed.path,
            caller_query=tuple(
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key.casefold() not in _OWNED_QUERY_KEYS
            ),
        )

    def page_request(self, page: int) -> Pazar3PageRequest:
        return self._page_request(page, _MAX_DISCOVERY_PAGE)

    def catalog_page_request(self, page: int) -> Pazar3PageRequest:
        return self._page_request(page, MAX_PAZAR3_CATALOG_PAGES)

    def _page_request(self, page: int, maximum_page: int) -> Pazar3PageRequest:
        if page not in range(1, maximum_page + 1):
            raise FeedFetchError(PAZAR3_LABEL, "InvalidPage")
        query = (*self.caller_query, ("Page", str(page)))
        return Pazar3PageRequest(
            scope=self,
            page=page,
            url=urlunsplit(
                (
                    self.scheme,
                    self.host,
                    self.configured_path,
                    urlencode(query, doseq=True),
                    "",
                ),
            ),
        )

    def accepts_redirect(
        self,
        request: Pazar3PageRequest,
        absolute_target_url: str,
    ) -> bool:
        trusted_target = _parse_listing_url(absolute_target_url)
        if trusted_target is None:
            return False
        target, target_host, target_port = trusted_target
        if (
            target.scheme != self.scheme
            or target_host != self.host
            or target_port != self.port
            or target.path != self.configured_path
        ):
            return False
        return _normalized_query(target.query) == _normalized_query(
            urlsplit(request.url).query,
        )


@dataclass(frozen=True, slots=True)
class Pazar3PageRequest:
    scope: Pazar3SearchScope
    page: int
    url: str


def _parse_listing_url(url: str) -> _ParsedListingUrl | None:
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = 443 if parsed.port is None else parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or port != 443
        or parsed.username is not None
        or parsed.password is not None
        or not is_canonical_pazar3_path(parsed.path, "/oglasi/")
        or "#" in url
    ):
        return None
    if host is None:
        return None
    return parsed, host, port


def is_canonical_pazar3_path(path: str, required_prefix: str) -> bool:
    if _INVALID_PERCENT_ESCAPE.search(path) or "\\" in path:
        return False
    folded_path = path.casefold()
    if "%2f" in folded_path or "%5c" in folded_path:
        return False
    try:
        decoded_path = unquote(path, errors="strict")
    except UnicodeDecodeError:
        return False
    segments = decoded_path.split("/")
    return decoded_path.startswith(required_prefix) and all(
        segment not in {".", ".."} for segment in segments
    )


def _normalized_query(query: str) -> Counter[tuple[str, str]]:
    return Counter(
        ("Page" if key.casefold() == "page" else key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
    )
