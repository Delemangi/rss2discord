import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field

import requests
from pydantic import JsonValue

from rss2discord.retries import FetchRetryPolicy

CATALOG_URL = "https://setec.mk/e-prodazba"


def no_wait_fetch_retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def catalog_scan_should_stop() -> bool:
    return False


def product_payload(
    product_id: str,
    handle: str,
    *,
    price: int | float | str = 1_499,
    original_price: int | float | str = 1_999,
) -> dict[str, JsonValue]:
    return {
        "id": product_id,
        "title": f"Product {product_id}",
        "handle": handle,
        "thumbnail": f"https://cdn.setec.mk/{product_id}.webp",
        "created_at": "2026-07-23T02:24:28.424Z",
        "variants": [
            {
                "calculated_price": {
                    "calculated_amount": price,
                    "original_amount": original_price,
                    "currency_code": "mkd",
                },
            },
        ],
        "categories": [{"name": "Computers"}, {"name": "Accessories"}],
    }


def catalog_payload(count: int, products: list[dict[str, JsonValue]]) -> bytes:
    return json.dumps({"count": count, "products": products}).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        if self.chunks is None:
            yield self.content
            return
        yield from self.chunks


class RecordingGet:
    """Return queued responses while recording mutable request history."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses: list[StubResponse] = responses
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []
        self.allow_redirects: list[bool] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del timeout, stream
        self.urls.append(url)
        self.headers.append(headers)
        self.allow_redirects.append(allow_redirects)
        return nullcontext(self.responses.pop(0))


@dataclass(frozen=True, slots=True)
class RaisingGet:
    error: requests.RequestException

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, stream, allow_redirects
        raise self.error
