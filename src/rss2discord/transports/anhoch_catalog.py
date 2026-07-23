"""Shared Anhoch catalog client and product models."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from typing import Final
from urllib.parse import urljoin

import requests
from pydantic import ValidationError

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.anhoch_catalog_bounds import (
    ANHOCH_LABEL,
    CatalogScanBounds,
    CatalogScanTotals,
    FetchedCatalogPage,
)
from rss2discord.transports.anhoch_catalog_url import catalog_base_url, page_url
from rss2discord.transports.anhoch_models import (
    AnhochCatalogResponse,
    AnhochProduct,
)
from rss2discord.transports.base import FeedFetchError

ANHOCH_PRODUCT_BASE_URL: Final = "https://www.anhoch.com/products/"
ANHOCH_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
ANHOCH_LATEST_PRODUCTS_PER_PAGE: Final = 30
ANHOCH_CATALOG_PRODUCTS_PER_PAGE: Final = 500
MAX_ANHOCH_LATEST_PAGES: Final = 3
MAX_ANHOCH_CATALOG_PAGES: Final = 100
MAX_ANHOCH_LATEST_RESPONSE_BYTES: Final = 1_048_576
MAX_ANHOCH_CATALOG_RESPONSE_BYTES: Final = 2_097_152
MAX_ANHOCH_LATEST_SCAN_BYTES: Final = 3_145_728
MAX_ANHOCH_CATALOG_SCAN_BYTES: Final = 67_108_864
ANHOCH_STREAM_CHUNK_BYTES: Final = 65_536
MAX_ANHOCH_REDIRECTS: Final = 10


class AnhochCatalogClient:
    """Fetch and validate Anhoch catalog pages."""

    def fetch_latest_products(self, url: str) -> tuple[AnhochProduct, ...]:
        return self._scan_products(
            url,
            per_page=ANHOCH_LATEST_PRODUCTS_PER_PAGE,
            max_pages=MAX_ANHOCH_LATEST_PAGES,
            max_response_bytes=MAX_ANHOCH_LATEST_RESPONSE_BYTES,
            max_scan_bytes=MAX_ANHOCH_LATEST_SCAN_BYTES,
            deduplicate=False,
            require_complete_catalog=False,
        )

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnhochProduct, ...]:
        full_catalog_url = catalog_base_url(url)
        return retry_policy.execute(
            lambda: self._scan_products(
                full_catalog_url,
                per_page=ANHOCH_CATALOG_PRODUCTS_PER_PAGE,
                max_pages=MAX_ANHOCH_CATALOG_PAGES,
                max_response_bytes=MAX_ANHOCH_CATALOG_RESPONSE_BYTES,
                max_scan_bytes=MAX_ANHOCH_CATALOG_SCAN_BYTES,
                deduplicate=True,
                require_complete_catalog=True,
                is_shutdown_requested=is_shutdown_requested,
            ),
        )

    def _scan_products(
        self,
        url: str,
        *,
        per_page: int,
        max_pages: int,
        max_response_bytes: int,
        max_scan_bytes: int,
        deduplicate: bool,
        require_complete_catalog: bool,
        is_shutdown_requested: Callable[[], bool] | None = None,
    ) -> tuple[AnhochProduct, ...]:
        products: list[AnhochProduct] = []
        seen_products: dict[int, AnhochProduct] = {}
        bounds = CatalogScanBounds(
            per_page=per_page,
            max_pages=max_pages,
            max_scan_bytes=max_scan_bytes,
            require_complete_catalog=require_complete_catalog,
        )
        totals = CatalogScanTotals(product_count=0, response_bytes=0)
        for page_number in range(1, max_pages + 1):
            if is_shutdown_requested is not None and is_shutdown_requested():
                raise FeedFetchInterruptedError
            if totals.response_bytes >= max_scan_bytes:
                raise FeedFetchError(ANHOCH_LABEL, "ScanResponseTooLarge")
            fetched_page = self._fetch_page(
                page_url(url, page_number=page_number, per_page=per_page),
                max_response_bytes=max_response_bytes,
                max_scan_bytes=max_scan_bytes - totals.response_bytes,
            )
            totals = bounds.validate_page(
                fetched_page,
                requested_page_number=page_number,
                totals=totals,
            )
            page = fetched_page.page
            if deduplicate:
                self._append_unique_products(products, seen_products, page.data)
            else:
                products.extend(page.data)
            if page.current_page >= page.last_page or not page.data:
                break
        return tuple(products)

    @staticmethod
    def _append_unique_products(
        products: list[AnhochProduct],
        seen_products: dict[int, AnhochProduct],
        page_products: tuple[AnhochProduct, ...],
    ) -> None:
        for product in page_products:
            existing_product = seen_products.get(product.id)
            if existing_product is None:
                seen_products[product.id] = product
                products.append(product)
                continue
            if existing_product != product:
                raise FeedFetchError(ANHOCH_LABEL, "DuplicateProductId")

    @classmethod
    def _fetch_page(
        cls,
        url: str,
        *,
        max_response_bytes: int,
        max_scan_bytes: int,
    ) -> FetchedCatalogPage:
        try:
            current_url = url
            response_bytes = 0
            for _ in range(MAX_ANHOCH_REDIRECTS + 1):
                with requests.get(
                    current_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": ANHOCH_USER_AGENT,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=30,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if location is None:
                            raise FeedFetchError(
                                ANHOCH_LABEL,
                                "InvalidRedirect",
                            ) from None
                        redirect_content = cls._read_content(
                            response,
                            max_response_bytes=max_response_bytes,
                            max_scan_bytes=max_scan_bytes - response_bytes,
                        )
                        response_bytes += len(redirect_content)
                        if response_bytes >= max_scan_bytes:
                            raise FeedFetchError(ANHOCH_LABEL, "ScanResponseTooLarge")
                        current_url = urljoin(current_url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except requests.HTTPError:
                        status_code = response.status_code
                        raise FeedFetchError(
                            ANHOCH_LABEL,
                            "HTTPError",
                            status_code=status_code,
                            retryable=(status_code == 429 or 500 <= status_code < 600),
                            retry_after=_parse_retry_after(
                                response.headers.get("Retry-After"),
                            ),
                        ) from None
                    content = cls._read_content(
                        response,
                        max_response_bytes=max_response_bytes,
                        max_scan_bytes=max_scan_bytes - response_bytes,
                    )
                    response_bytes += len(content)
                    break
            else:
                raise FeedFetchError(ANHOCH_LABEL, "TooManyRedirects") from None
        except ValueError:
            raise FeedFetchError(ANHOCH_LABEL, "InvalidUrl") from None
        except (requests.ConnectionError, requests.Timeout) as error:
            raise FeedFetchError(
                ANHOCH_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(ANHOCH_LABEL, type(error).__name__) from None
        try:
            page = AnhochCatalogResponse.model_validate_json(content).products
        except ValidationError:
            raise FeedFetchError(ANHOCH_LABEL, "InvalidResponse") from None
        return FetchedCatalogPage(page=page, response_bytes=response_bytes)

    @staticmethod
    def _read_content(
        response: requests.Response,
        *,
        max_response_bytes: int,
        max_scan_bytes: int,
    ) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > max_response_bytes:
                raise FeedFetchError(ANHOCH_LABEL, "ResponseTooLarge")
            if declared_bytes > max_scan_bytes:
                raise FeedFetchError(ANHOCH_LABEL, "ScanResponseTooLarge")
        content = bytearray()
        chunks: Iterator[bytes] = response.iter_content(
            chunk_size=ANHOCH_STREAM_CHUNK_BYTES,
        )
        for chunk in chunks:
            if len(content) + len(chunk) > max_response_bytes:
                raise FeedFetchError(ANHOCH_LABEL, "ResponseTooLarge")
            if len(content) + len(chunk) > max_scan_bytes:
                raise FeedFetchError(ANHOCH_LABEL, "ScanResponseTooLarge")
            content.extend(chunk)
        return bytes(content)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except ValueError:
        return None
    return retry_after if math.isfinite(retry_after) and retry_after >= 0 else None
