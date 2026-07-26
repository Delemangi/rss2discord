"""Bounded client for Neksio's public catalog endpoint."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import ValidationError

from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.neksio_catalog_http import (
    NEKSIO_LABEL,
    NEKSIO_PAGE_SIZE,
    NeksioCatalogRequest,
    fetch_homepage,
    fetch_page_content,
    origin_url,
)
from rss2discord.transports.neksio_models import (
    NeksioCatalogPage,
    NeksioProduct,
)

NEKSIO_FILTER_PATH: Final = "/FilterAndPaginateProducts"
MAX_NEKSIO_CATEGORIES: Final = 100
MAX_NEKSIO_PAGES_PER_CATEGORY: Final = 100
MAX_NEKSIO_PRODUCTS: Final = 10_000
_CATEGORY_TARGET: Final = re.compile(r"#subcat_([1-9][0-9]*)\Z")


class NeksioCatalogClient:
    """Fetch every validated Neksio catalog card in deterministic API order."""

    def fetch_catalog(self, url: str) -> tuple[NeksioProduct, ...]:
        """Fetch the complete catalog for every homepage category within fixed bounds."""
        origin = origin_url(url)
        observed_at = datetime.now(UTC)
        categories = _category_ids(fetch_homepage(origin))
        if len(categories) > MAX_NEKSIO_CATEGORIES:
            raise FeedFetchError(NEKSIO_LABEL, "CategoryLimitExceeded")

        products: list[NeksioProduct] = []
        seen_products: dict[int, NeksioProduct] = {}
        endpoint = urljoin(origin, NEKSIO_FILTER_PATH)
        for category_id in categories:
            self._append_category_products(
                endpoint,
                category_id,
                observed_at,
                products,
                seen_products,
            )
        return tuple(products)

    @classmethod
    def _append_category_products(
        cls,
        endpoint: str,
        category_id: int,
        observed_at: datetime,
        products: list[NeksioProduct],
        seen_products: dict[int, NeksioProduct],
    ) -> None:
        for page_number in range(1, MAX_NEKSIO_PAGES_PER_CATEGORY + 1):
            try:
                page = NeksioCatalogPage.model_validate_json(
                    fetch_page_content(
                        endpoint,
                        _catalog_request(category_id, page_number),
                    ),
                )
            except ValidationError:
                raise FeedFetchError(NEKSIO_LABEL, "InvalidResponse") from None
            _validate_page(page, category_id, page_number)
            for product_card in page.product_cards:
                product = product_card.observe(observed_at)
                existing_product = seen_products.get(product.id)
                if existing_product is None:
                    if len(products) >= MAX_NEKSIO_PRODUCTS:
                        raise FeedFetchError(NEKSIO_LABEL, "ProductLimitExceeded")
                    seen_products[product.id] = product
                    products.append(product)
                elif existing_product != product:
                    raise FeedFetchError(NEKSIO_LABEL, "DuplicateProductId")
            if page.no_of_products == 0 or page_number == page.no_of_pages:
                return
        raise FeedFetchError(NEKSIO_LABEL, "PageLimitExceeded")


def _catalog_request(category_id: int, page: int) -> NeksioCatalogRequest:
    return {
        "categoryId": category_id,
        "manufacturerIds": [],
        "subCategoryIds": [],
        "page": page,
        "pageSize": NEKSIO_PAGE_SIZE,
        "description": None,
        "selectedMinMaxPrice": {"minPrice": None, "maxPrice": None},
        "orderBy": 7,
        "quantityStock": 1,
    }


def _category_ids(content: bytes) -> tuple[int, ...]:
    categories: list[int] = []
    seen_categories: set[int] = set()
    soup = BeautifulSoup(content, "html.parser")
    for element in soup.select("div.side-menu-category[data-bs-target^='#subcat_']"):
        match = _CATEGORY_TARGET.fullmatch(str(element.get("data-bs-target")))
        if match is None:
            raise FeedFetchError(NEKSIO_LABEL, "MalformedCategoryMarkup")
        category_id = int(match.group(1))
        if category_id not in seen_categories:
            seen_categories.add(category_id)
            categories.append(category_id)
    if not categories:
        raise FeedFetchError(NEKSIO_LABEL, "EmptyCategoryEnumeration")
    return tuple(categories)


def _validate_page(page: NeksioCatalogPage, category_id: int, page_number: int) -> None:
    if page.category_id != category_id:
        raise FeedFetchError(NEKSIO_LABEL, "CategoryMismatch")
    if page.page != page_number:
        raise FeedFetchError(NEKSIO_LABEL, "PageNumberMismatch")
    if page.page_size != NEKSIO_PAGE_SIZE:
        raise FeedFetchError(NEKSIO_LABEL, "PageSizeMismatch")
    if page.no_of_pages > MAX_NEKSIO_PAGES_PER_CATEGORY:
        raise FeedFetchError(NEKSIO_LABEL, "PageLimitExceeded")
    if page.no_of_products == 0:
        if page.no_of_pages != 0 or page.product_cards:
            raise FeedFetchError(NEKSIO_LABEL, "PaginationMetadataMismatch")
        return
    expected_pages = -(-page.no_of_products // page.page_size)
    expected_cards = min(
        page.page_size,
        page.no_of_products - page.page_size * (page_number - 1),
    )
    if (
        page.no_of_pages != expected_pages
        or page_number > page.no_of_pages
        or len(page.product_cards) != expected_cards
    ):
        raise FeedFetchError(NEKSIO_LABEL, "PaginationMetadataMismatch")
