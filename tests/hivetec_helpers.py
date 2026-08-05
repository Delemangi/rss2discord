import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from pydantic import JsonValue

from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.hivetec_transport import HivetecCurlResponse

SHOP_URL = "https://hivetec.mk/shop/"


def no_wait_fetch_retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def catalog_scan_should_stop() -> bool:
    return False


def product_payload(
    product_id: int,
    *,
    price: str = "149900",
    regular_price: str = "199900",
    in_stock: bool = True,
) -> dict[str, JsonValue]:
    return {
        "id": product_id,
        "name": f"Product {product_id} &#8211; Gaming",
        "permalink": f"https://hivetec.mk/product/product-{product_id}/",
        "sku": f"SKU-{product_id}",
        "on_sale": price != regular_price,
        "prices": {
            "price": price,
            "regular_price": regular_price,
            "sale_price": price if price != regular_price else regular_price,
            "currency_code": "MKD",
            "currency_minor_unit": 2,
        },
        "images": [
            {
                "id": product_id,
                "src": f"https://hivetec.mk/wp-content/uploads/product-{product_id}.jpg",
                "thumbnail": f"https://hivetec.mk/wp-content/uploads/product-{product_id}-600x600.jpg",
                "name": f"product-{product_id}",
                "alt": "",
            },
        ],
        "categories": [
            {
                "id": 1,
                "name": "Computers",
                "slug": "computers",
                "link": "https://hivetec.mk/product-category/computers/",
            },
        ],
        "is_in_stock": in_stock,
    }


def products_payload(products: list[dict[str, JsonValue]]) -> bytes:
    return json.dumps(products).encode()


def dates_payload(product_ids: list[int]) -> bytes:
    return json.dumps(
        [
            {
                "id": product_id,
                "date_gmt": f"2026-08-04T{product_id % 24:02d}:00:00",
                "modified_gmt": f"2026-08-04T{product_id % 24:02d}:05:00",
                "status": "publish",
            }
            for product_id in product_ids
        ],
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None
    on_chunk: Callable[[], None] | None = None


class RecordingGet:
    """Return queued responses while recording request URLs."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.timeouts_ms: list[int] = []

    def __call__(
        self,
        url: str,
        *,
        timeout_ms: int,
        header_callback: Callable[[bytes], int],
        content_callback: Callable[[bytes], int],
    ) -> HivetecCurlResponse:
        self.urls.append(url)
        self.timeouts_ms.append(timeout_ms)
        response = self.responses.pop(0)
        header_chunks = [
            f"HTTP/1.1 {response.status_code}\r\n".encode(),
            *(
                f"{name}: {value}\r\n".encode("latin-1")
                for name, value in response.headers.items()
            ),
            b"\r\n",
        ]
        for chunk in header_chunks:
            if header_callback(chunk) == CURL_WRITEFUNC_ERROR:
                return HivetecCurlResponse(response.status_code, url)
        for chunk in response.chunks or (response.content,):
            if response.on_chunk is not None:
                response.on_chunk()
            if content_callback(chunk) == CURL_WRITEFUNC_ERROR:
                break
        return HivetecCurlResponse(response.status_code, url)
