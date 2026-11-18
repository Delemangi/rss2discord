import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import requests
from requests.structures import CaseInsensitiveDict

FAKULTETI_MK_URL = "https://fakulteti.mk/"
FAKULTETI_MK_SUBSET_URL = (
    "https://fakulteti.mk/category/obrazovanie,nauka,tehnologija,kariera"
)


def post(
    slug: str,
    title: str,
    date: str,
    *,
    category: str = "obrazovanie",
    description: str = "Description",
    image: str | None = None,
    comments: int = 0,
    shares: int = 0,
    url: str | None = None,
    category_name: str | None = None,
) -> dict[str, object]:
    return {
        "url": url or f"https://www.fakulteti.mk/news/{slug}",
        "title": title,
        "description": description,
        "imageUrl": image,
        "publicationDate": date,
        "category": {
            "id": category,
            "name": category_name or category.title(),
            "url": f"https://www.fakulteti.mk/category/{category}",
        },
        "facebook": {"comments": comments, "shares": shares},
    }


def json_payload(items: list[dict[str, object]]) -> bytes:
    return json.dumps(items).encode()


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
    """Return queued responses while recording request URLs and params."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
    ) -> StubResponse:
        del headers, timeout, stream
        self.calls.append((url, dict(params)))
        response = self.responses.pop(0)
        return StubResponse(
            response.content,
            response.status_code,
            CaseInsensitiveDict(response.headers),
        )
