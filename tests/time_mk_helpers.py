from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from html import escape

import requests
from requests.structures import CaseInsensitiveDict

TIME_MK_URL = "https://time.mk/"
TIME_MK_CATEGORY_URL = "https://time.mk/rss/tehnologija"
TIME_MK_SUBSET_URL = "https://time.mk/rss/tehnologija,ekonomija"


def article(
    title: str,
    source: str,
    link: str,
    published: str,
    category: str,
) -> str:
    return (
        "<item>"
        f"<title>{escape(title)} | {escape(source)}</title>"
        f"<link>{escape(link)}</link>"
        f"<guid>{escape(link)}</guid>"
        f"<pubDate>{published}</pubDate>"
        f"<category>{escape(category)}</category>"
        "</item>"
    )


def rss_payload(category: str, items: list[str]) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>TIME.mk - {escape(category)}</title>"
        "<link>https://time.mk/</link>"
        f"{''.join(items)}"
        "</channel></rss>"
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield self.content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback


class RecordingGet:
    """Return queued responses while recording request URLs."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
    ) -> StubResponse:
        del headers, timeout, stream
        self.urls.append(url)
        response = self.responses.pop(0)
        return StubResponse(
            response.content,
            response.status_code,
            CaseInsensitiveDict(response.headers),
        )


class DynamicGet:
    """Build one valid feed per requested category, keyed by the URL."""

    def __init__(self, article_builder: Callable[[str], str]) -> None:
        self._article_builder = article_builder
        self.urls: list[str] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
    ) -> StubResponse:
        del headers, timeout, stream
        self.urls.append(url)
        category = url.rsplit("/", 1)[-1]
        return StubResponse(
            rss_payload(category, [self._article_builder(category)]),
            headers=CaseInsensitiveDict({"Content-Type": "application/rss+xml"}),
        )
