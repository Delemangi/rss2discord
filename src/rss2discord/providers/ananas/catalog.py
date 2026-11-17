"""Bounded Ananas Algolia catalog access without HTML scraping."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    ANANAS_CATEGORY_PATH_PREFIX,
    ANANAS_COMPONENTS_FEED_URL,
    ANANAS_FEED_URL,
    ANANAS_GRAPHQL_ENDPOINT,
    ANANAS_GRAPHQL_MARKET,
    ANANAS_GRAPHQL_SOURCE_APP,
    ANANAS_HOST,
    ANANAS_IMAGE_HOST,
    ANANAS_JSON_CONTENT_TYPE,
    ANANAS_LABEL,
    ANANAS_LATEST_LOOKBACK_MS,
    ANANAS_MIN_COVERAGE_RATIO,
    ANANAS_ORIGIN,
    ANANAS_PAGE_SIZE,
    ANANAS_USER_AGENT,
    MAX_ANANAS_CATEGORY_RESPONSE_BYTES,
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
    "ANANAS_COMPONENTS_FEED_URL",
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
logger = logging.getLogger(__name__)

ANANAS_CATEGORY_QUERY: Final = """
query Categories($rootOnly: Boolean = true) {
  response: getCategories(rootOnly: $rootOnly) {
    ...CategoryFields
    children {
      ...CategoryFields
      children {
        ...CategoryFields
        children {
          ...CategoryFields
          children {
            ...CategoryFields
            children {
              ...CategoryFields
              children {
                ...CategoryFields
              }
            }
          }
        }
      }
    }
  }
}
fragment CategoryFields on Category {
  id
  name
  slug
}
"""


@dataclass(frozen=True, slots=True)
class _ResolvedCategory:
    slug: str
    path: tuple[str, ...]
    filter_expression: str


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
    """Validate any Ananas category URL under the public category tree."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(ANANAS_LABEL, "InvalidUrl") from None
    path = parsed.path.rstrip("/")
    prefix = ANANAS_CATEGORY_PATH_PREFIX
    if (
        parsed.scheme != "https"
        or parsed.hostname != ANANAS_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not path.startswith(prefix)
        or path == prefix.rstrip("/")
    ):
        raise FeedFetchError(ANANAS_LABEL, "InvalidUrl")
    slug = path[len(prefix) :]
    for segment in slug.split("/"):
        if (
            not segment
            or segment in {".", ".."}
            or any(character in segment for character in "/\\?#%")
            or any(
                character.isspace() or not character.isprintable()
                for character in segment
            )
        ):
            raise FeedFetchError(ANANAS_LABEL, "InvalidUrl")
    return urlunsplit(("https", ANANAS_HOST, path, "", ""))


class AnanasCatalogClient:
    """Traverse Ananas inventory pages through the public Algolia proxy."""

    def fetch_latest_products(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[AnanasProduct, ...]:
        url = validate_ananas_url(url)
        _require_search_key()
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
        url = validate_ananas_url(url)
        _require_search_key()
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
        category = self._resolve_category(validate_ananas_url(url), budget)
        filter_expression = category.filter_expression
        numeric_filters = (
            (f"firstPublishedTimestamp > {_latest_cutoff_ms()}",) if latest else ()
        )
        first_hits, total, page_count = self._fetch_page(
            0,
            filter_expression,
            numeric_filters,
            budget,
        )
        self._validate_scan_limits(total, page_count)
        if total == 0:
            budget.after_request()
            return ()
        grouped, seen_inventory = self._collect_pages(
            first_hits=first_hits,
            total=total,
            page_count=page_count,
            filter_expression=filter_expression,
            numeric_filters=numeric_filters,
            budget=budget,
            is_shutdown_requested=is_shutdown_requested,
        )
        coverage = len(seen_inventory) / total
        if coverage < 1.0:
            logger.warning(
                "Ananas catalog coverage %.4f (%s of %s inventory observations)",
                coverage,
                len(seen_inventory),
                total,
            )
        if coverage < ANANAS_MIN_COVERAGE_RATIO:
            raise FeedFetchError(ANANAS_LABEL, "IncompleteCatalog")
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

    def _resolve_category(self, url: str, budget: ScanBudget) -> _ResolvedCategory:
        slug = _category_slug(url)
        categories = self._fetch_category_tree(budget)
        path = _find_category_path(categories, slug)
        if path is None:
            raise FeedFetchError(ANANAS_LABEL, "UnknownCategory")
        return _ResolvedCategory(
            slug=slug,
            path=path,
            filter_expression=_category_filter(path),
        )

    def _fetch_category_tree(self, budget: ScanBudget) -> list[Any]:
        budget.before_request()
        callback = BoundedContentCallback.start(
            budget,
            max_bytes=MAX_ANANAS_CATEGORY_RESPONSE_BYTES,
            label=ANANAS_LABEL,
        )
        response = _perform_request(
            ANANAS_GRAPHQL_ENDPOINT,
            headers=_graphql_headers(),
            json_body={
                "query": ANANAS_CATEGORY_QUERY,
                "variables": {"rootOnly": False},
            },
            timeout=budget.request_timeout(),
            stream=False,
            allow_redirects=False,
            content_callback=callback.write,
        )
        _raise_callback_abort(callback)
        budget.after_request()
        _validate_response(response)
        content_type = (
            response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != ANANAS_JSON_CONTENT_TYPE:
            raise FeedFetchError(ANANAS_LABEL, "InvalidResponse")
        payload = _decode_json(callback.content)
        if not isinstance(payload, dict):
            raise FeedFetchError(ANANAS_LABEL, "InvalidCategoryTree")
        if payload.get("errors"):
            raise FeedFetchError(ANANAS_LABEL, "CategoryTreeError")
        data = payload.get("data")
        categories = data.get("response") if isinstance(data, dict) else None
        if not isinstance(categories, list):
            raise FeedFetchError(ANANAS_LABEL, "InvalidCategoryTree")
        return categories

    @staticmethod
    def _validate_scan_limits(total: int, page_count: int) -> None:
        if total > MAX_ANANAS_PRODUCTS:
            raise FeedFetchError(ANANAS_LABEL, "ProductLimitExceeded")
        if page_count > MAX_ANANAS_PAGES:
            raise FeedFetchError(ANANAS_LABEL, "PageLimitExceeded")

    def _collect_pages(
        self,
        *,
        first_hits: list[Any],
        total: int,
        page_count: int,
        filter_expression: str,
        numeric_filters: tuple[str, ...],
        budget: ScanBudget,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[dict[str, AnanasProduct], set[str]]:
        pages_to_fetch = page_count
        grouped: dict[str, AnanasProduct] = {}
        seen_inventory: set[str] = set()
        for page in range(pages_to_fetch):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            page_hits = self._page_hits(
                page=page,
                first_hits=first_hits,
                total=total,
                page_count=page_count,
                filter_expression=filter_expression,
                numeric_filters=numeric_filters,
                budget=budget,
            )
            expected = min(ANANAS_PAGE_SIZE, total - page * ANANAS_PAGE_SIZE)
            if len(page_hits) > ANANAS_PAGE_SIZE:
                raise FeedFetchError(ANANAS_LABEL, "InvalidPage")
            if len(page_hits) != expected:
                logger.debug(
                    "Ananas page cardinality changed: page=%s expected=%s observed=%s",
                    page,
                    expected,
                    len(page_hits),
                )
                if not page_hits:
                    break
            self._append_page(page_hits, grouped, seen_inventory, budget)
        return grouped, seen_inventory

    def _page_hits(
        self,
        *,
        page: int,
        first_hits: list[Any],
        total: int,
        page_count: int,
        filter_expression: str,
        numeric_filters: tuple[str, ...],
        budget: ScanBudget,
    ) -> list[Any]:
        if page == 0:
            return first_hits
        page_hits, page_total, page_pages = self._fetch_page(
            page,
            filter_expression,
            numeric_filters,
            budget,
        )
        if page_total != total or page_pages != page_count:
            logger.debug(
                "Ananas catalog metadata changed: page=%s expected_total=%s "
                "observed_total=%s expected_pages=%s observed_pages=%s",
                page,
                total,
                page_total,
                page_count,
                page_pages,
            )
        return page_hits

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
                logger.debug(
                    "Ananas duplicate inventory id skipped: %s",
                    product.inventory_id,
                )
                continue
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
            logger.debug(
                "Ananas final page metadata changed: expected_total=%s "
                "observed_total=%s expected_pages=%s observed_pages=%s",
                total,
                final_total,
                page_count,
                final_pages,
            )
        if len(final_hits) > ANANAS_PAGE_SIZE:
            raise FeedFetchError(ANANAS_LABEL, "InvalidPage")
        final_seen: set[str] = set()
        for item in final_hits:
            budget.before_chunk()
            product = AnanasCatalogClient._parse_hit(item)
            if product.inventory_id in final_seen:
                logger.debug(
                    "Ananas duplicate inventory id in final page skipped: %s",
                    product.inventory_id,
                )
                continue
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
        image_url = _image_url(product.get("coverImageUrl"))
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
        if content_type != ANANAS_JSON_CONTENT_TYPE:
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


def _require_search_key() -> str:
    api_key = os.environ.get(ANANAS_ALGOLIA_SEARCH_KEY_ENV, "").strip()
    if not api_key:
        raise FeedFetchError(ANANAS_LABEL, "MissingSearchKey")
    return api_key


def _request_headers() -> Mapping[str, str]:
    return {
        "Accept": ANANAS_JSON_CONTENT_TYPE,
        "Content-Type": ANANAS_JSON_CONTENT_TYPE,
        "User-Agent": ANANAS_USER_AGENT,
        "x-algolia-application-id": ANANAS_ALGOLIA_APPLICATION_ID,
        "x-algolia-api-key": _require_search_key(),
    }


def _graphql_headers() -> Mapping[str, str]:
    return {
        "Accept": ANANAS_JSON_CONTENT_TYPE,
        "Content-Type": ANANAS_JSON_CONTENT_TYPE,
        "User-Agent": ANANAS_USER_AGENT,
        "Accept-Language": "mk",
        "Origin": ANANAS_ORIGIN,
        "Referer": f"{ANANAS_ORIGIN}/",
        "x-ananas-market": ANANAS_GRAPHQL_MARKET,
        "x-source-app": ANANAS_GRAPHQL_SOURCE_APP,
    }


def _category_slug(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path[len(ANANAS_CATEGORY_PATH_PREFIX) :]


def _find_category_path(
    categories: list[Any],
    slug: str,
    ancestors: tuple[str, ...] = (),
) -> tuple[str, ...] | None:
    for item in categories:
        if not isinstance(item, dict):
            raise FeedFetchError(ANANAS_LABEL, "InvalidCategoryTree")
        name = item.get("name")
        category_slug = item.get("slug")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(category_slug, str)
            or not category_slug
        ):
            raise FeedFetchError(ANANAS_LABEL, "InvalidCategoryTree")
        path = (*ancestors, name.strip())
        if category_slug == slug:
            return path
        children = item.get("children")
        if children is None:
            continue
        if not isinstance(children, list):
            raise FeedFetchError(ANANAS_LABEL, "InvalidCategoryTree")
        found = _find_category_path(children, slug, path)
        if found is not None:
            return found
    return None


def _category_filter(path: tuple[str, ...]) -> str:
    level = len(path) - 1
    value = " > ".join(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'product.categories.lvl{level}:"{value}"'


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
            or parsed.hostname != ANANAS_IMAGE_HOST
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
            ANANAS_IMAGE_HOST,
            quote(decoded_path, safe="/-._~"),
            parsed.query,
            "",
        ),
    )
