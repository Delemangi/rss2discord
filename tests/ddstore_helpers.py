import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import requests
from curl_cffi import CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from pydantic import JsonValue

from rss2discord.retries import FetchRetryPolicy

CATALOG_URL = "https://ddstore.mk/catalog"


def no_wait_fetch_retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def catalog_scan_should_stop() -> bool:
    return False


def product_payload(
    product_id: str,
    *,
    created_at: str = "2024-07-09 08:54:25",
    price: int | float | str = 1_499,
    regular_price: int | float | str = 1_999,
    stock_status: str = "IN_STOCK",
) -> dict[str, JsonValue]:
    return {
        "uid": product_id,
        "sku": f"SKU-{product_id}",
        "name": f"Product {product_id}",
        "url_key": f"products/product-{product_id}",
        "url_suffix": ".html",
        "created_at": created_at,
        "stock_status": stock_status,
        "small_image": {"url": f"https://ddstore.mk/media/{product_id}.webp"},
        "categories": [
            {"uid": "category-1", "name": "Computers", "url_path": "computers"},
        ],
        "price_range": {
            "minimum_price": {
                "final_price": {"value": price, "currency": "MKD"},
                "regular_price": {"value": regular_price, "currency": "MKD"},
            },
        },
    }


def catalog_payload(
    total_count: int,
    items: list[dict[str, JsonValue]],
    *,
    current_page: int,
    page_size: int = 500,
    total_pages: int | None = None,
) -> bytes:
    resolved_total_pages = (
        total_pages
        if total_pages is not None
        else max(
            (total_count + page_size - 1) // page_size,
            1,
        )
    )
    return json.dumps(
        {
            "data": {
                "products": {
                    "total_count": total_count,
                    "items": items,
                    "page_info": {
                        "current_page": current_page,
                        "page_size": page_size,
                        "total_pages": resolved_total_pages,
                    },
                },
            },
        },
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None
    on_chunk: Callable[[], None] | None = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError


class RecordingPost:
    """Return queued responses while recording GraphQL request history."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.payloads: list[Mapping[str, JsonValue]] = []
        self.headers: list[Mapping[str, str]] = []
        self.curl_options: list[Mapping[CurlOpt, int]] = []
        self.allow_redirects: list[bool] = []

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> StubResponse:
        self.urls.append(url)
        self.payloads.append(json)
        self.headers.append(headers)
        self.curl_options.append({CurlOpt.TIMEOUT_MS: timeout_ms})
        self.allow_redirects.append(allow_redirects)
        response = self.responses.pop(0)
        chunks = response.chunks if response.chunks is not None else (response.content,)
        for chunk in chunks:
            if response.on_chunk is not None:
                response.on_chunk()
            if content_callback(chunk) == CURL_WRITEFUNC_ERROR:
                break
        return response

    def close(self) -> None:
        """Match curl-cffi Session cleanup."""
