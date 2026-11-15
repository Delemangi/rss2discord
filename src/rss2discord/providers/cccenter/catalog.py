"""Bounded CCCenter WooCommerce Store API catalog scraping."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from html import unescape
from typing import Any, Final, Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from curl_cffi import requests as curl_requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.cccenter.bounds import (
    CCCENTER_FEED_URL,
    CCCENTER_HOST,
    CCCENTER_LABEL,
    CCCENTER_ORIGIN,
    CCCENTER_PRODUCTS_PER_PAGE,
    CCCENTER_SHOP_PATH,
    CCCENTER_USER_AGENT,
    MAX_CCCENTER_PAGES,
    MAX_CCCENTER_PRODUCTS,
    MAX_CCCENTER_REQUESTS,
    MAX_CCCENTER_RESPONSE_BYTES,
    MAX_CCCENTER_SCAN_BYTES,
    MAX_CCCENTER_SCAN_SECONDS,
)
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.catalog_http import BoundedContentCallback
from rss2discord.transports.scan_budget import ScanBudget, ScanLimits

from .parser import (
    _merge_price_status,
    _safe_image_url,
    _safe_product_url,
    _store_display_fields,
    _store_price,
)

__all__ = [
    "CCCENTER_FEED_URL",
    "MAX_CCCENTER_DETAIL_PRODUCTS",
    "CCCenterCatalogClient",
    "validate_cccenter_url",
]

MAX_CCCENTER_DETAIL_PRODUCTS: Final = 10
_STORE_API: Final = f"{CCCENTER_ORIGIN}/?rest_route=/wc/store/v1/products"

CCCENTER_SCAN_LIMITS: Final = ScanLimits(
    label=CCCENTER_LABEL,
    max_seconds=MAX_CCCENTER_SCAN_SECONDS,
    max_requests=MAX_CCCENTER_REQUESTS,
    max_bytes=MAX_CCCENTER_SCAN_BYTES,
)


class _HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    encoding: str | None

    def raise_for_status(self) -> None: ...


def _perform_request(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    allow_redirects: bool,
    stream: bool,
    content_callback: Callable[[bytes], int],
) -> Any:  # noqa: ANN401
    return curl_requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=allow_redirects,
        stream=stream,
        content_callback=content_callback,
    )


def _validate_response(response: _HttpResponse) -> None:
    if 300 <= response.status_code < 400:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidRedirect")
    try:
        response.raise_for_status()
    except curl_requests.exceptions.HTTPError:
        status_code = response.status_code
        raise FeedFetchError(
            CCCENTER_LABEL,
            "HTTPError",
            status_code=status_code,
            retryable=(status_code == 429 or 500 <= status_code < 600),
        ) from None
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_CCCENTER_RESPONSE_BYTES:
            raise FeedFetchError(CCCENTER_LABEL, "ResponseTooLarge")


def validate_cccenter_url(url: str) -> str:
    """Validate the one supported CCCenter listing URL."""
    if url != CCCENTER_FEED_URL:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidUrl")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != CCCENTER_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != CCCENTER_SHOP_PATH
        or parsed.fragment
        or parse_qsl(parsed.query, keep_blank_values=True) != [("orderby", "date")]
    ):
        raise FeedFetchError(CCCENTER_LABEL, "InvalidUrl")
    return CCCENTER_FEED_URL


class CCCenterCatalogClient:
    """Enumerate the complete bounded listing index without N+1 detail fetches."""

    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        """Fetch at most ten selected details under one aggregate scan budget.

        Validate all source identities before any transfer; preserve input order
        and return no partial batch on interruption or failure. There are no
        internal retries or fresh per-product deadlines. Callers must still
        compare scalar status, currency and price with the selected listing.
        """
        budget = ScanBudget.start(CCCENTER_SCAN_LIMITS, is_shutdown_requested)
        budget.before_chunk()
        if len(listings) > MAX_CCCENTER_DETAIL_PRODUCTS:
            raise FeedFetchError(CCCENTER_LABEL, "DetailProductLimitExceeded")
        selected = tuple(listings)
        urls: list[str] = []
        api_ids: set[int] = set()
        for listing in selected:
            budget.before_chunk()
            url = _safe_product_url(listing.url)
            if listing.product_id != url:
                raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
            locator = listing.store_api_id
            if (
                isinstance(locator, bool)
                or not isinstance(locator, int)
                or locator <= 0
            ):
                raise FeedFetchError(CCCENTER_LABEL, "InvalidProductLocator")
            if url in urls:
                raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
            if locator in api_ids:
                raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
            urls.append(url)
            api_ids.add(locator)
        products: list[CCCenterProduct] = []
        for listing, url in zip(selected, urls, strict=True):
            budget.before_chunk()
            product = self._fetch_store_product(url, listing, budget)
            budget.after_request()
            products.append(product)
        budget.after_request()
        return tuple(products)

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        return self.fetch_catalog(url, is_shutdown_requested=is_shutdown_requested)

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        validate_cccenter_url(url)
        budget = ScanBudget.start(CCCENTER_SCAN_LIMITS, is_shutdown_requested)
        if retry_policy is None:
            return self._scan_catalog(url, is_shutdown_requested, budget)
        return retry_policy.execute(
            lambda: self._scan_catalog(url, is_shutdown_requested, budget),
            retry_guard=budget.require_retry_delay,
        )

    def _scan_catalog(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool],
        budget: ScanBudget,
    ) -> tuple[CCCenterProduct, ...]:
        validate_cccenter_url(url)
        first, total, page_count = self._fetch_store_page(1, budget)
        if total > MAX_CCCENTER_PRODUCTS:
            raise FeedFetchError(CCCENTER_LABEL, "ProductLimitExceeded")
        if page_count > MAX_CCCENTER_PAGES:
            raise FeedFetchError(CCCENTER_LABEL, "PageLimitExceeded")
        products: list[CCCenterProduct] = []
        seen_ids: set[tuple[str, str | int]] = set()
        for page in range(1, page_count + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if page == 1:
                cards = first
            else:
                # Fetch metadata alongside cards and require a stable collection.
                cards, page_total, page_pages = self._fetch_store_page(page, budget)
                if page_total != total or page_pages != page_count:
                    raise FeedFetchError(CCCENTER_LABEL, "CatalogMetadataDrift")
            expected = min(
                CCCENTER_PRODUCTS_PER_PAGE,
                total - (page - 1) * CCCENTER_PRODUCTS_PER_PAGE,
            )
            if len(cards) != expected:
                raise FeedFetchError(CCCENTER_LABEL, "IncompletePage")
            self._append_store_products(cards, products, seen_ids, budget)
        # Recheck first-page metadata after enumeration to detect collection drift.
        self._check_final_page(total, page_count, budget)
        budget.after_request()
        return tuple(products)

    def _check_final_page(
        self,
        total: int,
        page_count: int,
        budget: ScanBudget,
    ) -> None:
        final_cards, final_total, final_pages = self._fetch_store_page(1, budget)
        if final_total != total or final_pages != page_count:
            raise FeedFetchError(CCCENTER_LABEL, "CatalogMetadataDrift")
        if len(final_cards) != min(total, CCCENTER_PRODUCTS_PER_PAGE):
            raise FeedFetchError(CCCENTER_LABEL, "IncompletePage")
        final_seen: set[tuple[str, str | int]] = set()
        for item in final_cards:
            self._store_product(item, final_seen)

    def _append_store_products(
        self,
        cards: Sequence[Any],
        products: list[CCCenterProduct],
        seen_ids: set[tuple[str, str | int]],
        budget: ScanBudget,
    ) -> None:
        for item in cards:
            budget.before_chunk()
            products.append(self._store_product(item, seen_ids))

    @staticmethod
    def _store_product(
        item: Any,  # noqa: ANN401
        seen_ids: set[tuple[str, str | int]],
    ) -> CCCenterProduct:
        if not isinstance(item, dict):
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        raw_id = item.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        slug = item.get("slug")
        permalink = item.get("permalink")
        if not isinstance(slug, str) or not slug or not isinstance(permalink, str):
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        url = _safe_product_url(permalink)
        path_slug = urlsplit(url).path.removeprefix("/product/").removesuffix("/")
        if unquote(path_slug) != unquote(slug):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
        id_key: tuple[str, str | int] = ("id", raw_id)
        url_key: tuple[str, str | int] = ("url", url)
        if id_key in seen_ids or url_key in seen_ids:
            raise FeedFetchError(CCCENTER_LABEL, "DuplicateProduct")
        seen_ids.update((id_key, url_key))
        name = item.get("name")
        if not isinstance(name, str) or not unescape(name).strip():
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        current, original, status = _store_price(item)
        image, sku, categories = _store_display_fields(item)
        stock = item.get("is_in_stock")
        if not isinstance(stock, bool):
            raise FeedFetchError(CCCENTER_LABEL, "MalformedProduct")
        return CCCenterProduct(
            url,
            unescape(name).strip(),
            url,
            sku,
            current,
            original,
            _safe_image_url(image if isinstance(image, str) else None),
            categories,
            stock,
            price_status=status,
            store_api_id=raw_id,
        )

    @staticmethod
    def _fetch_store_page(page: int, budget: ScanBudget) -> tuple[list[Any], int, int]:
        query = urlencode(
            {
                "per_page": CCCENTER_PRODUCTS_PER_PAGE,
                "page": page,
                "orderby": "date",
                "order": "desc",
            },
        )
        response, content = CCCenterCatalogClient._fetch_json(
            f"{_STORE_API}&{query}",
            budget,
        )
        try:
            items = json.loads(content)
            total, pages = (
                int(response.headers["X-WP-Total"]),
                int(response.headers["X-WP-TotalPages"]),
            )
        except (ValueError, KeyError, TypeError):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse") from None
        if not isinstance(items, list) or total <= 0 or pages <= 0:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse")
        if total > MAX_CCCENTER_PRODUCTS:
            raise FeedFetchError(CCCENTER_LABEL, "ProductLimitExceeded")
        if pages > MAX_CCCENTER_PAGES:
            raise FeedFetchError(CCCENTER_LABEL, "PageLimitExceeded")
        if (
            pages
            != (total + CCCENTER_PRODUCTS_PER_PAGE - 1) // CCCENTER_PRODUCTS_PER_PAGE
            or page > pages
        ):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse")
        return items, total, pages

    @staticmethod
    def _fetch_store_product(
        url: str,
        listing: CCCenterListing | CCCenterProduct,
        budget: ScanBudget,
    ) -> CCCenterProduct:
        query = urlencode({"include": listing.store_api_id, "per_page": 1, "page": 1})
        response, content = CCCenterCatalogClient._fetch_json(
            f"{_STORE_API}&{query}",
            budget,
        )
        try:
            items = json.loads(content)
            total = int(response.headers["X-WP-Total"])
            pages = int(response.headers["X-WP-TotalPages"])
        except (ValueError, KeyError, TypeError):
            raise FeedFetchError(CCCENTER_LABEL, "InvalidDetail") from None
        if total != 1 or pages != 1:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidDetail")
        if not isinstance(items, list) or len(items) != 1:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidDetail")
        detail = CCCenterCatalogClient._store_product(items[0], set())
        if detail.store_api_id != listing.store_api_id or detail.url != url:
            raise FeedFetchError(CCCENTER_LABEL, "InvalidProductIdentity")
        return CCCenterProduct(
            listing.product_id,
            detail.name,
            listing.url,
            detail.sku,
            detail.current_price,
            detail.original_price,
            detail.image_url or listing.image_url,
            detail.categories,
            detail.is_in_stock,
            price_status=_merge_price_status(listing.price_status, detail.price_status),
            store_api_id=detail.store_api_id,
        )

    @staticmethod
    def _fetch_json(url: str, budget: ScanBudget) -> tuple[Any, str]:
        budget.before_request()
        callback = BoundedContentCallback.start(
            budget,
            max_bytes=MAX_CCCENTER_RESPONSE_BYTES,
            label=CCCENTER_LABEL,
        )
        response = CCCenterCatalogClient._request_json(url, budget, callback)
        _raise_callback_abort(callback)
        budget.after_request()
        _validate_response(response)
        content_type = (
            response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse")
        return response, _decode_html(response, callback.content)

    @staticmethod
    def _fetch_html(
        url: str,
        *,
        budget: ScanBudget | None = None,
    ) -> str:
        if budget is not None:
            budget.before_request()
        callback_state = BoundedContentCallback.start(
            budget,
            max_bytes=MAX_CCCENTER_RESPONSE_BYTES,
            label=CCCENTER_LABEL,
        )
        response = CCCenterCatalogClient._request_html(url, budget, callback_state)
        _raise_callback_abort(callback_state)
        if budget is not None:
            budget.after_request()
        _validate_response(response)
        return _decode_html(response, callback_state.content)

    @staticmethod
    def _request_html(
        url: str,
        budget: ScanBudget | None,
        callback_state: BoundedContentCallback,
    ) -> Any:  # noqa: ANN401
        try:
            timeout = budget.request_timeout() if budget is not None else 30.0
            return _perform_request(
                url,
                headers={"Accept": "text/html", "User-Agent": CCCENTER_USER_AGENT},
                timeout=timeout,
                stream=False,
                allow_redirects=False,
                content_callback=callback_state.write,
            )
        except FeedFetchError:
            raise
        except (curl_requests.exceptions.RequestException, ValueError) as error:
            if callback_state.abort_error is not None:
                raise callback_state.abort_error from None
            raise FeedFetchError(
                CCCENTER_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None

    @staticmethod
    def _request_json(
        url: str,
        budget: ScanBudget,
        callback_state: BoundedContentCallback,
    ) -> Any:  # noqa: ANN401
        try:
            return _perform_request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": CCCENTER_USER_AGENT,
                },
                timeout=budget.request_timeout(),
                stream=False,
                allow_redirects=False,
                content_callback=callback_state.write,
            )
        except FeedFetchError:
            raise
        except (curl_requests.exceptions.RequestException, ValueError) as error:
            if callback_state.abort_error is not None:
                raise callback_state.abort_error from None
            raise FeedFetchError(
                CCCENTER_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None


def _raise_callback_abort(callback_state: BoundedContentCallback) -> None:
    if callback_state.abort_error is not None:
        raise callback_state.abort_error from None


def _decode_html(response: _HttpResponse, content: bytearray) -> str:
    try:
        return bytes(content).decode(response.encoding or "utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FeedFetchError(CCCENTER_LABEL, "InvalidResponse") from None
