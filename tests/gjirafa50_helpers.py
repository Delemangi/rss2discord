import json
from collections.abc import Iterator, Mapping, Sequence
from decimal import Decimal
from types import TracebackType

import requests

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
    def __init__(self, content: bytes, *, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.url = "https://gjirafa50.mk/product/search"

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def iter_content(self, _chunk_size: int) -> Iterator[bytes]:
        yield self.content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)


class RecordingGet:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.params: list[Mapping[str, str | int]] = []

    def __call__(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> StubResponse:
        del url, headers, timeout, stream, allow_redirects
        self.params.append(params)
        return self.responses.pop(0)


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
