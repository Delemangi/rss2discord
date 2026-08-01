import html
import json
from collections.abc import Iterator, Mapping
from decimal import Decimal

import requests
from pydantic import JsonValue

from rss2discord.retries import FetchRetryPolicy

CATEGORY_URL = "https://www.neptun.mk/KOMPJUTERI.nspx"


def product_payload(
    product_id: int,
    *,
    title: str | None = None,
    actual_price: Decimal | int = 1_999,
    date_inserted: str = "2026-07-21T15:44:34.97",
) -> dict[str, JsonValue]:
    serialized_price: int | str = (
        str(actual_price) if isinstance(actual_price, Decimal) else actual_price
    )
    return {
        "Id": product_id,
        "Title": title or f"Product {product_id}",
        "AvailableOnline": True,
        "AvailableWebshop": True,
        "Active": True,
        "Manufacturer": {"Name": "Lenovo"},
        "Category": {"Name": "Computers", "Url": "KOMPJUTERI"},
        "CodeNumber": f"CODE-{product_id}",
        "HasDiscount": actual_price != 2_499,
        "RegularPrice": 2_499,
        "DiscountPrice": serialized_price,
        "WebshopDiscountPrice": serialized_price,
        "ActualPrice": serialized_price,
        "DiscountPercent": 20,
        "Currency": "ден.",
        "Thumbnail": f"Content/Images/{product_id}.jpg",
        "Url": f"product-{product_id}",
        "Warranty": 12,
        "Quantity": 3,
        "Preorder": False,
        "DateInserted": date_inserted,
    }


def products_payload(total: int, products: list[dict[str, JsonValue]]) -> bytes:
    return json.dumps(
        {"Batch": {"Config": {"TotalItems": total}, "Items": products}},
    ).encode()


def category_html(*, models: int = 1, category_id: int = 2) -> bytes:
    model = html.escape(
        json.dumps(
            {
                "CategoryId": category_id,
                "Sort": 1,
                "Recomended": False,
                "ShowAllProducts": False,
                "ItemsPerPage": 20,
                "CurrentPage": 4,
            },
        ),
        quote=True,
    )
    elements = "".join(
        f'<div id="angularApp" data-initialsearchmodel="{model}"></div>'
        for _ in range(models)
    )
    return f"<html><body>{elements}</body></html>".encode()


class StubResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {} if headers is None else dict(headers)

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield self.content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError


class TruncatedResponse(StubResponse):
    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield self.content
        raise requests.exceptions.ChunkedEncodingError


class RecordingRequests:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Mapping[str, JsonValue] | None]] = []

    def get(self, url: str, **kwargs: object) -> StubResponse:
        self.calls.append(("GET", url, None))
        del kwargs
        return self.responses.pop(0)

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        **kwargs: object,
    ) -> StubResponse:
        self.calls.append(("POST", url, json))
        del kwargs
        return self.responses.pop(0)


def no_wait_retry_policy(
    errors: list[str] | None = None,
) -> FetchRetryPolicy:
    recorded = [] if errors is None else errors
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: recorded.append(error.cause_type),
    )


def never_shutdown() -> bool:
    return False


def requested_page(payload: Mapping[str, JsonValue] | None) -> int:
    if payload is None:
        raise AssertionError
    model = payload.get("model")
    if not isinstance(model, dict):
        raise TypeError
    page = model.get("CurrentPage")
    if not isinstance(page, int):
        raise TypeError
    return page
