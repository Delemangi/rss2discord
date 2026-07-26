"""Bounded client for Neksio's public catalog endpoint."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pydantic import ValidationError

from rss2discord.retries import FeedFetchInterruptedError
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
MAX_NEKSIO_CATEGORY_ID: Final = 2**63 - 1
_CATEGORY_TARGET: Final = re.compile(r"#subcat_([1-9][0-9]{0,18})\Z")


@dataclass(slots=True)
class _CatalogScan:
    """Mutable catalog state accumulated across one complete scan."""

    endpoint: str
    observed_at: datetime
    products: list[NeksioProduct]
    seen_products: dict[int, NeksioProduct]


class NeksioCatalogClient:
    """Fetch every validated Neksio catalog card in deterministic API order."""

    def fetch_catalog(
        self,
        url: str,
        *,
        is_shutdown_requested: Callable[[], bool] | None = None,
    ) -> tuple[NeksioProduct, ...]:
        """Fetch the complete catalog for every homepage category within fixed bounds."""
        origin = origin_url(url)
        observed_at = datetime.now(UTC)
        categories = _category_ids(fetch_homepage(origin))
        if len(categories) > MAX_NEKSIO_CATEGORIES:
            raise FeedFetchError(NEKSIO_LABEL, "CategoryLimitExceeded")

        scan = _CatalogScan(
            endpoint=urljoin(origin, NEKSIO_FILTER_PATH),
            observed_at=observed_at,
            products=[],
            seen_products={},
        )
        for category_id in categories:
            self._append_category_products(scan, category_id, is_shutdown_requested)
        return tuple(scan.products)

    @staticmethod
    def _append_category_products(
        scan: _CatalogScan,
        category_id: int,
        is_shutdown_requested: Callable[[], bool] | None,
    ) -> None:
        pagination: tuple[int, int] | None = None
        for page_number in range(1, MAX_NEKSIO_PAGES_PER_CATEGORY + 1):
            if is_shutdown_requested is not None and is_shutdown_requested():
                raise FeedFetchInterruptedError
            try:
                page = NeksioCatalogPage.model_validate_json(
                    fetch_page_content(
                        scan.endpoint,
                        _catalog_request(category_id, page_number),
                    ),
                )
            except ValidationError:
                raise FeedFetchError(NEKSIO_LABEL, "InvalidResponse") from None
            page_pagination = (page.no_of_pages, page.no_of_products)
            if pagination is not None and page_pagination != pagination:
                raise FeedFetchError(NEKSIO_LABEL, "PaginationMetadataDrift")
            pagination = page_pagination
            _validate_page(page, category_id, page_number)
            for product_card in page.product_cards:
                product = product_card.observe(scan.observed_at)
                existing_product = scan.seen_products.get(product.product_id)
                if existing_product is None:
                    if len(scan.products) >= MAX_NEKSIO_PRODUCTS:
                        raise FeedFetchError(NEKSIO_LABEL, "ProductLimitExceeded")
                    scan.seen_products[product.product_id] = product
                    scan.products.append(product)
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
        if category_id > MAX_NEKSIO_CATEGORY_ID:
            raise FeedFetchError(NEKSIO_LABEL, "MalformedCategoryMarkup")
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
