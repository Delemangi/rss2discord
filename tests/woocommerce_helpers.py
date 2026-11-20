import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import cast

import requests
from pydantic import JsonValue
from requests.structures import CaseInsensitiveDict

from rss2discord.retries import FetchRetryPolicy

SHOP_URL = "https://technosapien.mk/"
CATEGORY_URL = "https://technosapien.mk/product-category/komponenti/ssd-hdd/"


def no_wait_fetch_retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def product_payload(
    product_id: int,
    *,
    price: str = "149900",
    regular_price: str = "199900",
    in_stock: bool = True,
    host: str = "technosapien.mk",
    permalink: str | None = None,
    images: list[dict[str, JsonValue]] | None = None,
) -> dict[str, JsonValue]:
    default_images: list[dict[str, JsonValue]] = [
        {
            "id": product_id,
            "src": f"https://{host}/wp-content/uploads/product-{product_id}.jpg",
            "thumbnail": (
                f"https://{host}/wp-content/uploads/product-{product_id}-300x300.jpg"
            ),
        },
    ]
    return {
        "id": product_id,
        "name": f"Product {product_id} &#8211; Gaming",
        "permalink": permalink or f"https://{host}/product/product-{product_id}/",
        "sku": f"SKU-{product_id}",
        "type": "simple",
        "prices": {
            "price": price,
            "regular_price": regular_price,
            "currency_code": "MKD",
            "currency_minor_unit": 2,
        },
        "images": cast(JsonValue, images if images is not None else default_images),
        "categories": [
            {
                "id": 75,
                "name": "Графички Карти",
                "slug": "%d0%b3%d1%80%d0%b0%d1%84%d0%b8%d1%87%d0%ba%d0%b8-%d0%ba%d0%b0%d1%80%d1%82%d0%b8",
            },
        ],
        "is_in_stock": in_stock,
    }


def products_payload(products: list[dict[str, JsonValue]]) -> bytes:
    return json.dumps(products).encode()


def categories_payload(categories: list[dict[str, JsonValue]]) -> bytes:
    return json.dumps(categories).encode()


def category_payload(category_id: int, name: str, slug: str) -> dict[str, JsonValue]:
    return {"id": category_id, "name": name, "slug": slug}


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
    """Return queued responses while recording request URLs and headers."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
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
    ) -> StubResponse:
        del timeout, stream, allow_redirects
        self.urls.append(url)
        self.headers.append(headers)
        response = self.responses.pop(0)
        return StubResponse(
            response.content,
            response.status_code,
            CaseInsensitiveDict(response.headers),
        )
