import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import requests

from rss2discord.retries import FetchRetryPolicy

CATALOG_URL = (
    "https://www.anhoch.com/products?query=keyboard&inStockOnly=2&sort=price_asc"
)

type PayloadValue = (
    str | int | Decimal | None | Mapping[str, PayloadValue] | Sequence[PayloadValue]
)
type ProductPayload = dict[str, PayloadValue]


def no_wait_fetch_retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def catalog_scan_should_stop() -> bool:
    return False


def product_payload(
    product_id: int,
    slug: str,
    *,
    selling_price: str = "1.200,00 ден.",
    selling_price_amount: str | Decimal = "1200.00",
    selling_price_currency: str = "MKD",
    price_amount: str | Decimal = "1500.00",
    price_currency: str = "MKD",
) -> ProductPayload:
    return {
        "id": product_id,
        "name": f"Product {product_id}",
        "slug": slug,
        "price": {
            "amount": price_amount,
            "currency": price_currency,
            "formatted": "1.500,00 ден.",
        },
        "selling_price": {
            "amount": selling_price_amount,
            "currency": selling_price_currency,
            "formatted": selling_price,
        },
        "base_image": {"path": f"https://www.anhoch.com/images/{product_id}.jpg"},
        "is_in_stock": True,
        "qty": 7,
        "installments": {
            "period": 24,
            "price": {
                "amount": "50.00",
                "currency": "MKD",
                "formatted": "50,00 ден.",
            },
        },
    }


def page_payload(
    current_page: int,
    last_page: int,
    products: Sequence[Mapping[str, PayloadValue]],
) -> bytes:
    return json.dumps(
        {
            "products": {
                "current_page": current_page,
                "last_page": last_page,
                "data": products,
            },
        },
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
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

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del timeout, stream, allow_redirects
        self.urls.append(url)
        self.headers.append(headers)
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


@dataclass(frozen=True, slots=True)
class RedirectingGet:
    redirect: StubResponse
    final: StubResponse

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool = True,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, stream
        return nullcontext(self.final if allow_redirects else self.redirect)


def requested_page_numbers(urls: Sequence[str]) -> list[str]:
    return [
        query["page"][0] for query in (parse_qs(urlsplit(url).query) for url in urls)
    ]
