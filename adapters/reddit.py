"""Credential-free Reddit RSS normalization."""

import re
from collections.abc import Mapping
from dataclasses import replace
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from models import EntryData


class RedditAdapter:
    """Normalize Reddit RSS links and authors without OAuth metadata."""

    def adapt(self, entry: Any, data: EntryData) -> EntryData:  # noqa: ANN401
        outbound_link = _outbound_link(entry)
        link = data.link
        discussion_url = data.discussion_url
        if outbound_link is not None and outbound_link != data.link:
            link = outbound_link
            discussion_url = data.discussion_url or data.link

        author = re.sub(r"^/?u/", "", data.author, flags=re.IGNORECASE)
        return replace(
            data,
            link=link,
            discussion_url=discussion_url,
            author=author,
        )


class _RedditLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.link_target: str | None = None
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        self._current_href = next(
            (value for name, value in attrs if name.casefold() == "href"),
            None,
        )
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._current_href is None:
            return
        if "".join(self._current_text).strip().casefold() == "[link]":
            self.link_target = self._current_href
        self._current_href = None
        self._current_text = []


def _outbound_link(entry: Any) -> str | None:  # noqa: ANN401
    content = entry.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, Mapping):
            continue
        value = item.get("value")
        if not isinstance(value, str):
            continue
        parser = _RedditLinkParser()
        parser.feed(value)
        if parser.link_target is not None and _is_http_url(parser.link_target):
            return parser.link_target
    return None


def _is_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and parsed.hostname is not None
