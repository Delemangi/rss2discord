import json
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.retries import FetchRetryPolicy

ROOT_URL = "https://gjirafa50.mk/"


def product_card(product_id: int, price: int | Decimal) -> str:
    formatted = f"{Decimal(price):.2f}".replace(".", ",")
    return (
        f'<div class="product-item" data-productid="{product_id}" '
        f'data-discountedprice="{formatted}">'
        '<section class="picture"><a href="/product-'
        f'{product_id}"><img src="https://50cdn.gjirafamall.tech/{product_id}.jpg" '
        f'alt="Product {product_id}"></a></section>'
        '<h3 class="product-title"><a href="/product-'
        f'{product_id}">Product {product_id}</a></h3>'
        f'<span class="price main">{formatted} MKD.</span></div>'
    )


def catalog_payload(
    total: int,
    products: Sequence[tuple[int, int | Decimal]],
    *,
    total_pages: int | None = None,
) -> bytes:
    pages = total_pages if total_pages is not None else (total + 23) // 24
    return json.dumps(
        {
            "totalpages": pages,
            "totalHits": total,
            "productsCount": len(products),
            "html": "".join(product_card(*product) for product in products),
        },
    ).encode()


class StubResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        raw_header_lines: tuple[bytes, ...] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.raw_header_lines = raw_header_lines
        self.url = "https://gjirafa50.mk/product/search"


class RecordingGet:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.params: list[Mapping[str, str | int]] = []
        self.timeouts: list[int] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        header_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> StubResponse:
        del url, headers, allow_redirects
        self.params.append(params)
        self.timeouts.append(timeout_ms)
        response = self.responses.pop(0)
        header_lines = response.raw_header_lines or (
            f"HTTP/1.1 {response.status_code} Test\r\n".encode(),
            *(
                f"{name}: {value}\r\n".encode()
                for name, value in response.headers.items()
            ),
            b"\r\n",
        )
        for line in header_lines:
            if header_callback(line) == CURL_WRITEFUNC_ERROR:
                return response
        if content_callback(response.content) == CURL_WRITEFUNC_ERROR:
            return response
        return response

    def close(self) -> None:
        return


def no_wait_retry_policy() -> FetchRetryPolicy:
    def sleep(seconds: float) -> bool:
        del seconds
        return True

    def on_retry(error: Exception, delay: float) -> None:
        del error, delay

    return FetchRetryPolicy(
        sleep=sleep,
        on_retry=on_retry,
    )
