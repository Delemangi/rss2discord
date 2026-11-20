"""Bounded first-party HTTP boundary for WooCommerce Store API requests."""

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Final
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from pydantic import TypeAdapter, ValidationError

from rss2discord.providers.woocommerce.bounds import (
    MAX_WOOCOMMERCE_CATEGORIES,
    MAX_WOOCOMMERCE_CATEGORY_PAGES,
    MAX_WOOCOMMERCE_REDIRECTS,
    MAX_WOOCOMMERCE_RESPONSE_BYTES,
    WOOCOMMERCE_CATEGORIES_API_PATH,
    WOOCOMMERCE_LABEL,
    WOOCOMMERCE_PRODUCT_FIELDS,
    WOOCOMMERCE_STORE_API_PATH,
    WOOCOMMERCE_USER_AGENT,
    WooCommercePageRequest,
    WooCommerceShop,
)
from rss2discord.providers.woocommerce.models import (
    WooCommerceCategory,
    WooCommerceProduct,
)
from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.bounded_http import read_bounded_content

WOOCOMMERCE_STREAM_CHUNK_BYTES: Final = 64 * 1024
MAX_WOOCOMMERCE_RESPONSE_SECONDS: Final = 30


@dataclass(frozen=True, slots=True)
class FetchedWooCommerceProducts:
    products: tuple[WooCommerceProduct, ...]
    total: int
    total_pages: int
    response_bytes: int


def normalize_shop_url(url: str) -> WooCommerceShop:
    """Validate one HTTPS shop root, shop page, or product-category URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidUrl") from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or _is_forbidden_hostname(hostname)
    ):
        raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidUrl")
    path = parsed.path.rstrip("/") or "/"
    category_slug: str | None = None
    category_prefix = next(
        (
            prefix
            for prefix in ("/product-category/", "/kategori/")
            if path.startswith(prefix)
        ),
        None,
    )
    if path == "/shop":
        path = "/"
    elif category_prefix is not None:
        segments = [
            unquote(segment)
            for segment in path.removeprefix(category_prefix).split("/")
            if segment
        ]
        if not segments:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidUrl")
        category_slug = segments[-1]
        if not category_slug:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidUrl")
    elif path != "/":
        raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidUrl")
    return WooCommerceShop(
        origin=urlunsplit(("https", hostname, "", "", "")),
        hostname=hostname,
        category_slug=category_slug,
    )


class WooCommerceHttpClient:
    """Retrieve Store API pages without trusting redirects or sizes."""

    def __init__(self, shop: WooCommerceShop) -> None:
        self._shop = shop

    def resolve_category_id(self, slug: str) -> int:
        categories = self._fetch_categories()
        matches = [
            category
            for category in categories
            if category.slug and unquote(category.slug) == slug
        ]
        if len(matches) != 1:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "UnknownCategory")
        return matches[0].id

    def _fetch_categories(self) -> tuple[WooCommerceCategory, ...]:
        categories: list[WooCommerceCategory] = []
        seen: set[int] = set()
        for page in range(1, MAX_WOOCOMMERCE_CATEGORY_PAGES + 1):
            query = urlencode(
                {
                    "page": page,
                    "per_page": 100,
                    "_fields": "id,name,slug",
                },
            )
            content, headers = self._request(
                f"{self._shop.origin}{WOOCOMMERCE_CATEGORIES_API_PATH}?{query}",
                remaining_scan_bytes=MAX_WOOCOMMERCE_RESPONSE_BYTES,
            )
            try:
                page_categories = TypeAdapter(
                    tuple[WooCommerceCategory, ...],
                ).validate_json(content)
                total_pages = int(headers.get("x-wp-totalpages", "1"))
            except (ValueError, ValidationError):
                raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidResponse") from None
            for category in page_categories:
                if category.id in seen:
                    raise FeedFetchError(WOOCOMMERCE_LABEL, "DuplicateCategoryId")
                seen.add(category.id)
                categories.append(category)
                if len(categories) > MAX_WOOCOMMERCE_CATEGORIES:
                    raise FeedFetchError(WOOCOMMERCE_LABEL, "CategoryLimitExceeded")
            if page >= total_pages:
                return tuple(categories)
        raise FeedFetchError(WOOCOMMERCE_LABEL, "CategoryLimitExceeded")

    def fetch_products_page(
        self,
        request: WooCommercePageRequest,
    ) -> FetchedWooCommerceProducts:
        query: dict[str, str | int] = {
            "page": request.page,
            "per_page": request.per_page,
            "orderby": "date",
            "order": "desc",
            "_fields": WOOCOMMERCE_PRODUCT_FIELDS,
        }
        if request.category_id is not None:
            query["category"] = request.category_id
        content, headers = self._request(
            f"{self._shop.origin}{WOOCOMMERCE_STORE_API_PATH}?{urlencode(query)}",
            remaining_scan_bytes=request.remaining_scan_bytes,
        )
        try:
            products = TypeAdapter(tuple[WooCommerceProduct, ...]).validate_json(
                content,
            )
            total = int(headers["x-wp-total"])
            total_pages = int(headers["x-wp-totalpages"])
        except (KeyError, ValueError, ValidationError):
            raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidResponse") from None
        if total < 0 or total_pages < 0:
            raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidResponse")
        return FetchedWooCommerceProducts(products, total, total_pages, len(content))

    def _request(
        self,
        url: str,
        *,
        remaining_scan_bytes: int,
    ) -> tuple[bytes, Mapping[str, str]]:
        current_url = url
        consumed_bytes = 0
        try:
            for _ in range(MAX_WOOCOMMERCE_REDIRECTS + 1):
                response_context = requests.get(
                    current_url,
                    headers={
                        "Accept": "application/json",
                        "Referer": f"{self._shop.origin}/",
                        "User-Agent": WOOCOMMERCE_USER_AGENT,
                    },
                    timeout=MAX_WOOCOMMERCE_RESPONSE_SECONDS,
                    stream=True,
                    allow_redirects=False,
                )
                with response_context as response:
                    content = _read_content(
                        response,
                        remaining_scan_bytes=remaining_scan_bytes - consumed_bytes,
                    )
                    consumed_bytes += len(content)
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if location is None:
                            raise FeedFetchError(
                                WOOCOMMERCE_LABEL,
                                "InvalidRedirect",
                            )
                        current_url = _same_origin_redirect_url(
                            current_url,
                            location,
                            self._shop.hostname,
                        )
                        continue
                    _check_response_status(response)
                    return content, response.headers
            raise FeedFetchError(WOOCOMMERCE_LABEL, "TooManyRedirects")
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as error:
            raise FeedFetchError(
                WOOCOMMERCE_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(
                WOOCOMMERCE_LABEL,
                type(error).__name__,
            ) from None


def _check_response_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError:
        status = response.status_code
        raise FeedFetchError(
            WOOCOMMERCE_LABEL,
            "HTTPError",
            status_code=status,
            retryable=status in {408, 429} or 500 <= status < 600,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        ) from None


def _read_content(
    response: requests.Response,
    *,
    remaining_scan_bytes: int,
) -> bytes:
    return read_bounded_content(
        response,
        label=WOOCOMMERCE_LABEL,
        max_bytes=MAX_WOOCOMMERCE_RESPONSE_BYTES,
        chunk_bytes=WOOCOMMERCE_STREAM_CHUNK_BYTES,
        remaining_bytes=remaining_scan_bytes,
    )


def _same_origin_redirect_url(
    current_url: str,
    location: str,
    hostname: str,
) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        redirected = urlsplit(redirected_url)
        port = redirected.port
    except ValueError:
        raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != "https"
        or redirected.hostname != hostname
        or port not in {None, 443}
        or redirected.username is not None
        or redirected.password is not None
        or redirected.fragment
    ):
        raise FeedFetchError(WOOCOMMERCE_LABEL, "InvalidRedirect")
    return redirected_url


def _is_forbidden_hostname(hostname: str) -> bool:
    lowered = hostname.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        ip_address(lowered)
    except ValueError:
        return False
    return True
