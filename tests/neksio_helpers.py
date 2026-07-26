import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from decimal import Decimal
from typing import NotRequired, TypedDict

import requests

CATALOG_URL = "https://g.store.neksio.mk/not-the-homepage?ignored=true"
PAGE_SIZE = 100


class PriceRangePayload(TypedDict):
    minPrice: None
    maxPrice: None


class CatalogRequestPayload(TypedDict):
    categoryId: int
    manufacturerIds: list[int]
    subCategoryIds: list[int]
    page: int
    pageSize: int
    description: None
    selectedMinMaxPrice: PriceRangePayload
    orderBy: int
    quantityStock: int


class ProductCardPayload(TypedDict):
    productId: int
    productName: str
    productCode: str
    category: str
    subCategory: str
    manufacturer: str
    priceWTax: int | Decimal
    priceWTax_f: str
    old_PriceWTax: NotRequired[str | None]
    imagePath: str
    quantity: int


def catalog_request(category_id: int, page: int) -> CatalogRequestPayload:
    return {
        "categoryId": category_id,
        "manufacturerIds": [],
        "subCategoryIds": [],
        "page": page,
        "pageSize": PAGE_SIZE,
        "description": None,
        "selectedMinMaxPrice": {"minPrice": None, "maxPrice": None},
        "orderBy": 7,
        "quantityStock": 1,
    }


def homepage_payload(category_ids: Sequence[int]) -> bytes:
    categories = "".join(
        f'<div class="side-menu-category" data-bs-target="#subcat_{category_id}"></div>'
        for category_id in category_ids
    )
    return categories.encode()


def product_card(
    product_id: int,
    *,
    name: str | None = None,
    old_price: str | None = None,
) -> ProductCardPayload:
    return {
        "productId": product_id,
        "productName": name if name is not None else f"Product {product_id}",
        "productCode": f"CODE-{product_id}",
        "category": "Components",
        "subCategory": "Graphics",
        "manufacturer": "Example",
        "priceWTax": 1_200,
        "priceWTax_f": "1.200 ден.",
        "old_PriceWTax": old_price,
        "imagePath": f"/images/{product_id}.png",
        "quantity": 7,
    }


def page_payload(
    category_id: int,
    page: int,
    no_of_pages: int,
    no_of_products: int,
    product_cards: Sequence[ProductCardPayload],
    *,
    page_size: int = PAGE_SIZE,
) -> bytes:
    return json.dumps(
        {
            "categoryId": category_id,
            "page": page,
            "pageSize": page_size,
            "noOfPages": no_of_pages,
            "noOfProducts": no_of_products,
            "productCards": product_cards,
        },
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    chunks: tuple[bytes, ...] | None = None
    interruption: requests.RequestException | None = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield from self.chunks if self.chunks is not None else (self.content,)
        if self.interruption is not None:
            raise self.interruption


class RecordingGet:
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
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del headers, timeout, stream, allow_redirects
        self.urls.append(url)
        return nullcontext(self.responses.pop(0))


class RecordingPost:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []
        self.bodies: list[CatalogRequestPayload] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
        json: CatalogRequestPayload,
    ) -> AbstractContextManager[StubResponse]:
        del headers, timeout, stream, allow_redirects
        self.urls.append(url)
        self.bodies.append(json)
        return nullcontext(self.responses.pop(0))


@dataclass(frozen=True, slots=True)
class RaisingPost:
    error: requests.RequestException

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
        json: CatalogRequestPayload,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, stream, allow_redirects, json
        raise self.error
