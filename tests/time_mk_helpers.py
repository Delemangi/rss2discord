from collections.abc import Callable, Mapping
from html import escape

from requests.structures import CaseInsensitiveDict

from tests.fakes import StubResponse

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


def details_payload(description: str, image: str | None) -> bytes:
    metas = f'<meta property="og:description" content="{escape(description)}">'
    if image is not None:
        metas += f'<meta property="og:image" content="{escape(image)}">'
    return f"<html><head>{metas}</head><body></body></html>".encode()


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
