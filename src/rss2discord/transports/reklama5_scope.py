from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from rss2discord.transports.base import FeedFetchError

REKLAMA5_LABEL: Final = "Reklama5"

_ALLOWED_HOSTS: Final = frozenset({"reklama5.mk", "www.reklama5.mk"})
_ALLOWED_PATHS: Final = frozenset(
    {"/Search", "/Search/", "/Search/Index", "/Search/Index/"},
)
_OWNED_QUERY_KEYS: Final = frozenset({"sortbyprice", "pageview", "page"})
_CANONICAL_QUERY_KEYS: Final = (
    ("sortbyprice", "SortByPrice"),
    ("pageview", "pageView"),
    ("page", "page"),
)

type _ParsedSearchUrl = tuple[SplitResult, str, int]


@dataclass(frozen=True, slots=True)
class Reklama5SearchScope:
    scheme: str
    host: str
    port: int
    configured_path: str
    caller_query: tuple[tuple[str, str], ...]

    @classmethod
    def from_url(cls, url: str) -> Reklama5SearchScope:
        trusted_url = _parse_search_url(url)
        if trusted_url is None:
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidUrl")
        parsed, host, port = trusted_url
        caller_query = tuple(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _OWNED_QUERY_KEYS
        )
        return cls(
            scheme=parsed.scheme,
            host=host,
            port=port,
            configured_path=parsed.path,
            caller_query=caller_query,
        )

    def page_request(self, page: int) -> Reklama5PageRequest:
        if page not in range(1, 4):
            raise FeedFetchError(REKLAMA5_LABEL, "InvalidPage")
        query = (
            *self.caller_query,
            ("SortByPrice", "2"),
            ("pageView", "1"),
            ("page", str(page)),
        )
        url = urlunsplit(
            (
                self.scheme,
                self.host,
                self.configured_path,
                urlencode(query, doseq=True),
                "",
            ),
        )
        return Reklama5PageRequest(scope=self, page=page, url=url)

    def accepts_redirect(
        self,
        request: Reklama5PageRequest,
        absolute_target_url: str,
    ) -> bool:
        trusted_target = _parse_search_url(absolute_target_url)
        if trusted_target is None:
            return False
        target, target_host, target_port = trusted_target
        if (
            target.scheme != self.scheme
            or target_host != self.host
            or target_port != self.port
        ):
            return False
        requested_query = urlsplit(request.url).query
        return _normalized_query(target.query) == _normalized_query(requested_query)


@dataclass(frozen=True, slots=True)
class Reklama5PageRequest:
    scope: Reklama5SearchScope
    page: int
    url: str


def _parse_search_url(url: str) -> _ParsedSearchUrl | None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed_port = parsed.port
        port = 443 if parsed_port is None else parsed_port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_HOSTS
        or port != 443
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in _ALLOWED_PATHS
        or "#" in url
    ):
        return None
    if host is None:
        return None
    return parsed, host, port


def _normalized_query(query: str) -> Counter[tuple[str, str]]:
    return Counter(
        (_canonical_query_key(key), value)
        for key, value in parse_qsl(query, keep_blank_values=True)
    )


def _canonical_query_key(key: str) -> str:
    folded_key = key.casefold()
    return next(
        (
            canonical_key
            for owned_key, canonical_key in _CANONICAL_QUERY_KEYS
            if folded_key == owned_key
        ),
        key,
    )
