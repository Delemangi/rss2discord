"""Bounded Ananas Algolia catalog access without HTML scraping."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from html import unescape
from typing import Any, Final, Protocol, TypeGuard
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from curl_cffi import requests as curl_requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import (
    PriceAmountValidationError,
    canonicalize_price_amount,
)
from rss2discord.providers.ananas.bounds import (
    ANANAS_ALGOLIA_APPLICATION_ID,
    ANANAS_ALGOLIA_ENDPOINT,
    ANANAS_ALGOLIA_SEARCH_KEY_ENV,
    ANANAS_ATTRIBUTES_TO_RETRIEVE,
    ANANAS_CATEGORY_FILTERS,
    ANANAS_FEED_URL,
    ANANAS_HOST,
    ANANAS_LABEL,
    ANANAS_LATEST_LOOKBACK_MS,
    ANANAS_ORIGIN,
    ANANAS_PAGE_SIZE,
    ANANAS_USER_AGENT,
    MAX_ANANAS_LATEST_PAGES,
    MAX_ANANAS_PAGES,
    MAX_ANANAS_PRODUCTS,
    MAX_ANANAS_REQUESTS,
    MAX_ANANAS_RESPONSE_BYTES,
    MAX_ANANAS_SCAN_BYTES,
    MAX_ANANAS_SCAN_SECONDS,
)
from rss2discord.providers.ananas.models import AnanasProduct
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports.catalog_http import BoundedContentCallback
from rss2discord.transports.scan_budget import ScanBudget, ScanLimits

__all__ = [
    "ANANAS_FEED_URL",
    "ANANAS_SCAN_LIMITS",
    "AnanasCatalogClient",
    "validate_ananas_url",
]

ANANAS_SCAN_LIMITS: Final = ScanLimits(
    label=ANANAS_LABEL,
    max_seconds=MAX_ANANAS_SCAN_SECONDS,
    max_requests=MAX_ANANAS_REQUESTS,
    max_bytes=MAX_ANANAS_SCAN_BYTES,
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
    json_body: Mapping[str, object],
    timeout: float,
    allow_redirects: bool,
    stream: bool,
    content_callback: Callable[[bytes], int],
) -> Any:  # noqa: ANN401
    return curl_requests.post(
        url,
        headers=headers,
        json=dict(json_body),
        timeout=timeout,
        allow_redirects=allow_redirects,
        stream=stream,
        content_callback=content_callback,
    )


def _validate_response(response: _HttpResponse) -> None:
    if 300 <= response.status_code < 400:
        raise FeedFetchError(ANANAS_LABEL, "InvalidRedirect")
    try:
        response.raise_for_status()
    except curl_requests.exceptions.HTTPError:
        status_code = response.status_code
        raise FeedFetchError(
            ANANAS_LABEL,
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
        if declared_bytes > MAX_ANANAS_RESPONSE_BYTES:
            raise FeedFetchError(ANANAS_LABEL, "ResponseTooLarge")


def validate_ananas_url(url: str) -> str:
    """Validate one of the explicitly supported Ananas category URLs."""
    if url not in ANANAS_CATEGORY_FILTERS:
        raise FeedFetchError(ANANAS_LABEL, "InvalidUrl")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(ANANAS_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != ANANAS_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise FeedFetchError(ANANAS_LABEL, "InvalidUrl")
    return url


class AnanasCatalogClient:
    """Traverse Ananas inventory pages through the public Algolia proxy."""

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[AnanasProduct, ...]:
        validate_ananas_url(url)
        budget = ScanBudget.start(ANANAS_SCAN_LIMITS, is_shutdown_requested)
        return self._scan_catalog(
            url,
            is_shutdown_requested,
            budget,
            latest=True,
        )

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[AnanasProduct, ...]:
        validate_ananas_url(url)
        budget = ScanBudget.start(ANANAS_SCAN_LIMITS, is_shutdown_requested)
        if retry_policy is None:
            return self._scan_catalog(
                url,
                is_shutdown_requested,
                budget,
                latest=False,
            )
        return retry_policy.execute(
            lambda: self._scan_catalog(
                url,
                is_shutdown_requested,
                budget,
                latest=False,
            ),
            retry_guard=budget.require_retry_delay,
        )

    def _scan_catalog(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool],
        budget: ScanBudget,
        *,
        latest: bool,
    ) -> tuple[AnanasProduct, ...]:
        filter_expression = ANANAS_CATEGORY_FILTERS[validate_ananas_url(url)]
        numeric_filters = (
            (f"firstPublishedTimestamp > {_latest_cutoff_ms()}",) if latest else ()
        )
        first_hits, total, page_count = self._fetch_page(
            0,
            filter_expression,
            numeric_filters,
            budget,
        )
        if total > MAX_ANANAS_PRODUCTS:
            raise FeedFetchError(ANANAS_LABEL, "ProductLimitExceeded")
        if not latest and page_count > MAX_ANANAS_PAGES:
            raise FeedFetchError(ANANAS_LABEL, "PageLimitExceeded")
        if total == 0:
            budget.after_request()
            return ()
        pages_to_fetch = (
            min(page_count, MAX_ANANAS_LATEST_PAGES) if latest else page_count
        )
        grouped: dict[str, AnanasProduct] = {}
        seen_inventory: set[str] = set()
        for page in range(pages_to_fetch):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if page == 0:
                page_hits = first_hits
            else:
                page_hits, page_total, page_pages = self._fetch_page(
                    page,
                    filter_expression,
                    numeric_filters,
                    budget,
                )
                if page_total != total or page_pages != page_count:
                    raise FeedFetchError(ANANAS_LABEL, "CatalogMetadataDrift")
            expected = min(ANANAS_PAGE_SIZE, total - page * ANANAS_PAGE_SIZE)
            if len(page_hits) != expected:
                raise FeedFetchError(ANANAS_LABEL, "IncompletePage")
            self._append_page(page_hits, grouped, seen_inventory, budget)
        if not latest:
            self._check_final_page(
                filter_expression,
                numeric_filters,
                total,
                page_count,
                budget,
            )
        budget.after_request()
        return tuple(grouped.values())

    @staticmethod
    def _append_page(
        hits: list[Any],
        grouped: dict[str, AnanasProduct],
        seen_inventory: set[str],
        budget: ScanBudget,
    ) -> None:
        for item in hits:
            budget.before_chunk()
            product = AnanasCatalogClient._parse_hit(item)
            if product.inventory_id in seen_inventory:
                raise FeedFetchError(ANANAS_LABEL, "DuplicateInventory")
            seen_inventory.add(product.inventory_id)
            grouped[product.product_id] = _select_offer(
                grouped.get(product.product_id),
                product,
            )

    @staticmethod
    def _check_final_page(
        filter_expression: str,
        numeric_filters: tuple[str, ...],
        total: int,
        page_count: int,
        budget: ScanBudget,
    ) -> None:
        final_hits, final_total, final_pages = AnanasCatalogClient._fetch_page(
            0,
            filter_expression,
            numeric_filters,
            budget,
        )
        if final_total != total or final_pages != page_count:
            raise FeedFetchError(ANANAS_LABEL, "CatalogMetadataDrift")
        if len(final_hits) != min(total, ANANAS_PAGE_SIZE):
            raise FeedFetchError(ANANAS_LABEL, "IncompletePage")
        final_seen: set[str] = set()
        for item in final_hits:
            budget.before_chunk()
            product = AnanasCatalogClient._parse_hit(item)
            if product.inventory_id in final_seen:
                raise FeedFetchError(ANANAS_LABEL, "DuplicateInventory")
            final_seen.add(product.inventory_id)

    @staticmethod
    def _parse_hit(item: Any) -> AnanasProduct:  # noqa: ANN401
        if not isinstance(item, dict):
            raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
        inventory_id = _inventory_id(item.get("objectID"))
        price = _required_price(item.get("price"))
        base_price = _optional_price(item.get("basePrice"))
        on_stock = item.get("onStock", False)
        if not isinstance(on_stock, bool):
            raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
        available = _optional_available(item.get("available"))
        first_published_ms = _optional_positive_int(
            item.get("firstPublishedTimestamp"),
        )
        merchant = _merchant(item.get("merchant"))
        product = item.get("product")
        if not isinstance(product, dict):
            raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
        raw_name = product.get("name")
        if not isinstance(raw_name, str) or not unescape(raw_name).strip():
            raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
        slug = _safe_segment(product.get("slug"), cause="MalformedProduct")
        has_variations = product.get("hasVariations", False)
        if not isinstance(has_variations, bool):
            raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
        image_url = _image_url(product.get("thumbnailUrl")) or _image_url(
            product.get("coverImageUrl"),
        )
        return AnanasProduct(
            inventory_id=inventory_id,
            product_id=slug,
            name=unescape(raw_name).strip(),
            url=(
                f"{ANANAS_ORIGIN}/proizvod/"
                f"{quote(slug, safe='-._~')}/{quote(inventory_id, safe='-._~')}"
            ),
            image_url=image_url,
            brand=_optional_text(product.get("brand")),
            categories=_categories(product.get("categoryNames")),
            price=price,
            base_price=base_price,
            currency="MKD",
            in_stock=on_stock,
            available=available,
            merchant=merchant,
            first_published_ms=first_published_ms,
            has_variations=has_variations,
        )

    @staticmethod
    def _fetch_page(
        page: int,
        filter_expression: str,
        numeric_filters: tuple[str, ...],
        budget: ScanBudget,
    ) -> tuple[list[Any], int, int]:
        budget.before_request()
        callback = BoundedContentCallback.start(
            budget,
            max_bytes=MAX_ANANAS_RESPONSE_BYTES,
            label=ANANAS_LABEL,
        )
        response = AnanasCatalogClient._request_json(
            page,
            filter_expression,
            numeric_filters,
            budget,
            callback,
        )
        _raise_callback_abort(callback)
        budget.after_request()
        _validate_response(response)
        content_type = (
            response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
        payload = _decode_json(callback.content)
        return _parse_page(payload, page)

    @staticmethod
    def _request_json(
        page: int,
        filter_expression: str,
        numeric_filters: tuple[str, ...],
        budget: ScanBudget,
        callback_state: BoundedContentCallback,
    ) -> Any:  # noqa: ANN401
        try:
            return _perform_request(
                ANANAS_ALGOLIA_ENDPOINT,
                headers=_request_headers(),
                json_body=_request_body(
                    page,
                    filter_expression,
                    numeric_filters,
                ),
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
                ANANAS_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None


def _request_headers() -> Mapping[str, str]:
    api_key = os.environ.get(ANANAS_ALGOLIA_SEARCH_KEY_ENV, "").strip()
    if not api_key:
        raise FeedFetchError(ANANAS_LABEL, "MissingSearchKey")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": ANANAS_USER_AGENT,
        "x-algolia-application-id": ANANAS_ALGOLIA_APPLICATION_ID,
        "x-algolia-api-key": api_key,
    }


def _request_body(
    page: int,
    filter_expression: str,
    numeric_filters: tuple[str, ...],
) -> dict[str, object]:
    body: dict[str, object] = {
        "query": "",
        "hitsPerPage": ANANAS_PAGE_SIZE,
        "page": page,
        "filters": filter_expression,
        "attributesToRetrieve": list(ANANAS_ATTRIBUTES_TO_RETRIEVE),
    }
    if numeric_filters:
        body["numericFilters"] = list(numeric_filters)
    return body


def _parse_page(payload: Any, expected_page: int) -> tuple[list[Any], int, int]:  # noqa: ANN401
    if not isinstance(payload, dict):
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    hits = payload.get("hits")
    total = payload.get("nbHits")
    page_count = payload.get("nbPages")
    page = payload.get("page")
    hits_per_page = payload.get("hitsPerPage")
    if not isinstance(hits, list):
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    if not _is_int(total) or total < 0:
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    if not _is_int(page_count) or page_count < 0:
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    if not _is_int(page) or page != expected_page:
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    if not _is_int(hits_per_page) or hits_per_page != ANANAS_PAGE_SIZE:
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    if total == 0:
        if page_count != 0 or hits:
            raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    elif page_count != (total + ANANAS_PAGE_SIZE - 1) // ANANAS_PAGE_SIZE:
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
    return hits, total, page_count


def _decode_json(content: bytearray) -> Any:  # noqa: ANN401
    try:
        return json.loads(bytes(content).decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FeedFetchError(ANANAS_LABEL, "InvalidResponse") from None


def _latest_cutoff_ms() -> int:
    return int(time.time() * 1000) - ANANAS_LATEST_LOOKBACK_MS


def _select_offer(
    current: AnanasProduct | None,
    candidate: AnanasProduct,
) -> AnanasProduct:
    """Prefer the lowest-priced in-stock offer, then the lowest price."""
    if current is None:
        return candidate
    if current.in_stock != candidate.in_stock:
        return candidate if candidate.in_stock else current
    if candidate.price < current.price:
        return candidate
    if (
        candidate.price == current.price
        and candidate.inventory_id < current.inventory_id
    ):
        return candidate
    return current


def _raise_callback_abort(callback_state: BoundedContentCallback) -> None:
    if callback_state.abort_error is not None:
        raise callback_state.abort_error from None


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _inventory_id(value: object) -> str:
    if isinstance(value, bool):
        raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
    if isinstance(value, int):
        if value <= 0:
            raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
        return str(value)
    if isinstance(value, str):
        return _safe_segment(value, cause="MalformedProduct")
    raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")


def _safe_segment(value: object, *, cause: str) -> str:
    if not isinstance(value, str):
        raise FeedFetchError(ANANAS_LABEL, cause)
    text = value.strip()
    if (
        not text
        or text in {".", ".."}
        or any(character in text for character in "/\\?#%")
        or any(character.isspace() or not character.isprintable() for character in text)
    ):
        raise FeedFetchError(ANANAS_LABEL, cause)
    return text


def _required_price(value: object) -> Decimal:
    amount = _price_amount(value)
    if amount <= 0:
        raise FeedFetchError(ANANAS_LABEL, "MalformedPrice")
    return amount


def _optional_price(value: object) -> Decimal | None:
    if value is None:
        return None
    amount = _price_amount(value)
    if amount < 0:
        raise FeedFetchError(ANANAS_LABEL, "MalformedPrice")
    return amount if amount > 0 else None


def _price_amount(value: object) -> Decimal:
    # Algolia serializes whole MKD prices as JSON floats (for example 7403.0).
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FeedFetchError(ANANAS_LABEL, "MalformedPrice")
    if isinstance(value, float) and not math.isfinite(value):
        raise FeedFetchError(ANANAS_LABEL, "MalformedPrice")
    amount = Decimal(str(value))
    try:
        canonicalize_price_amount(amount)
    except PriceAmountValidationError:
        raise FeedFetchError(ANANAS_LABEL, "MalformedPrice") from None
    return amount


def _optional_available(value: object) -> int | None:
    if value is None:
        return None
    if not _is_int(value):
        raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
    if value < 0:
        raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
    return value


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if not _is_int(value):
        raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
    if value <= 0:
        raise FeedFetchError(ANANAS_LABEL, "MalformedProduct")
    return value


def _merchant(value: object) -> str:
    if isinstance(value, dict):
        display_name = value.get("displayName")
        if isinstance(display_name, str) and display_name.strip():
            return display_name.strip()
    return ANANAS_LABEL


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _categories(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        text for item in value if isinstance(item, str) and (text := item.strip())
    )


def _image_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("//"):
        return None
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return None
    if parsed.fragment:
        return None
    if parsed.scheme:
        if (
            parsed.scheme != "https"
            or parsed.hostname != ANANAS_HOST
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
    elif parsed.netloc or not parsed.path.startswith("/"):
        return None
    decoded_path = unquote(parsed.path)
    if (
        "\\" in parsed.path
        or "\\" in decoded_path
        or decoded_path.count("/") != parsed.path.count("/")
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    ):
        return None
    return urlunsplit(
        (
            "https",
            ANANAS_HOST,
            quote(decoded_path, safe="/-._~"),
            parsed.query,
            "",
        ),
    )
