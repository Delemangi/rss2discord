"""Bounded HTTP catalog client for PROXNet category pages."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

import requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.proxnet.bounds import (
    MAX_PROXNET_LATEST_PAGES,
    MAX_PROXNET_PAGES,
    MAX_PROXNET_PRODUCTS,
    MAX_PROXNET_RESPONSE_BYTES,
    MAX_PROXNET_SCAN_BYTES,
    PROXNET_CATEGORY_PATH_PREFIX,
    PROXNET_HOST,
    PROXNET_INTER_REQUEST_DELAY,
    PROXNET_LABEL,
    PROXNET_PRODUCTS_PER_PAGE,
    PROXNET_REQUEST_TIMEOUT,
    PROXNET_USER_AGENT,
)
from rss2discord.providers.proxnet.models import ProxnetPage, ProxnetProduct
from rss2discord.providers.proxnet.parser import parse_proxnet_page
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    parse_retry_after,
)
from rss2discord.transports.bounded_http import read_bounded_content

__all__ = [
    "ProxnetCatalogClient",
    "page_url",
    "parse_proxnet_page",
    "validate_proxnet_url",
]

_STREAM_CHUNK_BYTES: Final = 65_536
_ALLOWED_HOSTS: Final = frozenset({"www.proxnet.mk", "proxnet.mk"})
_RESERVED_CATEGORY_SLUGS: Final = frozenset(
    {
        "account",
        "addresses",
        "authentication",
        "cart",
        "contact-us",
        "discount",
        "guest-tracking",
        "identity",
        "login",
        "module",
        "my-account",
        "order",
        "order-confirmation",
        "password",
        "prices-drop",
        "registration",
        "search",
        "sitemap",
        "stores",
    },
)


def validate_proxnet_url(url: str) -> str:
    """Validate one credential-free PROXNet category URL and canonicalize it."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(PROXNET_LABEL, "InvalidUrl") from None
    path = parsed.path.rstrip("/")
    category = path.removeprefix(PROXNET_CATEGORY_PATH_PREFIX)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not path.startswith(PROXNET_CATEGORY_PATH_PREFIX)
        or "/" in category
        or not _is_category_slug(category)
        or category in _RESERVED_CATEGORY_SLUGS
        or path.casefold().endswith(".html")
    ):
        raise FeedFetchError(PROXNET_LABEL, "InvalidUrl")
    return f"https://{PROXNET_HOST}{path}"


def _is_category_slug(value: str) -> bool:
    if not value:
        return False
    return all(
        part.isascii() and part.isalnum() and (part.islower() or part.isdigit())
        for part in value.split("-")
    )


def page_url(base_url: str, page_number: int) -> str:
    """Build the provider-owned newest-first pagination query for one page."""
    return (
        f"{base_url}?orderby=date_add&orderway=desc"
        f"&n={PROXNET_PRODUCTS_PER_PAGE}&p={page_number}"
    )


class ProxnetCatalogClient:
    """Enumerate bounded PROXNet category pages in newest-first order."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

    def fetch_latest(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> ProxnetPage:
        """Fetch up to two newest-first pages and deduplicate their products."""
        base_url = validate_proxnet_url(url)
        products: list[ProxnetProduct] = []
        seen: set[str] = set()
        title = ""
        consumed_bytes = 0
        for page_number in range(1, MAX_PROXNET_LATEST_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if page_number > 1:
                self._sleep(PROXNET_INTER_REQUEST_DELAY)
            page, page_bytes = self._fetch_page(
                page_url(base_url, page_number),
                remaining_bytes=MAX_PROXNET_SCAN_BYTES - consumed_bytes,
            )
            consumed_bytes += page_bytes
            if page_number == 1:
                title = page.title
            if not page.products:
                break
            _append_unique(products, seen, page.products)
            if len(products) > MAX_PROXNET_PRODUCTS:
                raise FeedFetchError(PROXNET_LABEL, "ProductLimitExceeded")
        return ProxnetPage(products=tuple(products), title=title)

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[ProxnetProduct, ...]:
        """Fetch every category page newest-first until empty or bounded."""
        base_url = validate_proxnet_url(url)

        def scan() -> tuple[ProxnetProduct, ...]:
            return self._scan_catalog(base_url, is_shutdown_requested)

        if retry_policy is None:
            return scan()
        return retry_policy.execute(scan)

    def _scan_catalog(
        self,
        base_url: str,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[ProxnetProduct, ...]:
        products: list[ProxnetProduct] = []
        seen: set[str] = set()
        consumed_bytes = 0
        for page_number in range(1, MAX_PROXNET_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if page_number > 1:
                self._sleep(PROXNET_INTER_REQUEST_DELAY)
            page, page_bytes = self._fetch_page(
                page_url(base_url, page_number),
                remaining_bytes=MAX_PROXNET_SCAN_BYTES - consumed_bytes,
            )
            consumed_bytes += page_bytes
            _append_unique(products, seen, page.products)
            if len(products) > MAX_PROXNET_PRODUCTS:
                raise FeedFetchError(PROXNET_LABEL, "ProductLimitExceeded")
            if not page.products:
                break
        return tuple(products)

    def _fetch_page(
        self,
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[ProxnetPage, int]:
        html = self._fetch_html(url, remaining_bytes=remaining_bytes)
        return parse_proxnet_page(html), len(html.encode("utf-8"))

    @staticmethod
    def _fetch_html(url: str, *, remaining_bytes: int) -> str:
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "text/html",
                    "User-Agent": PROXNET_USER_AGENT,
                },
                timeout=PROXNET_REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
        except requests.Timeout as error:
            raise FeedFetchError(
                PROXNET_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.ConnectionError as error:
            raise FeedFetchError(
                PROXNET_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.TooManyRedirects as error:
            raise FeedFetchError(
                PROXNET_LABEL,
                type(error).__name__,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(
                PROXNET_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except ValueError:
            raise FeedFetchError(PROXNET_LABEL, "InvalidUrl") from None
        try:
            _validate_final_url(getattr(response, "url", url))
            _check_response_status(response)
            content = read_bounded_content(
                response,
                label=PROXNET_LABEL,
                max_bytes=MAX_PROXNET_RESPONSE_BYTES,
                chunk_bytes=_STREAM_CHUNK_BYTES,
                remaining_bytes=remaining_bytes,
            )
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    return content.decode(response.encoding or "utf-8")
                except (LookupError, UnicodeDecodeError):
                    raise FeedFetchError(PROXNET_LABEL, "InvalidResponse") from None
        finally:
            response.close()


def _append_unique(
    products: list[ProxnetProduct],
    seen: set[str],
    page_products: tuple[ProxnetProduct, ...],
) -> None:
    for product in page_products:
        if product.product_id in seen:
            continue
        seen.add(product.product_id)
        products.append(product)


def _validate_final_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise FeedFetchError(PROXNET_LABEL, "InvalidRedirect") from None
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise FeedFetchError(PROXNET_LABEL, "InvalidRedirect")


def _check_response_status(response: requests.Response) -> None:
    status_code = response.status_code
    if 300 <= status_code < 400:
        raise FeedFetchError(PROXNET_LABEL, "InvalidRedirect")
    if status_code < 400:
        return
    raise FeedFetchError(
        PROXNET_LABEL,
        "HTTPError",
        status_code=status_code,
        retryable=status_code == 429 or 500 <= status_code < 600,
        retry_after=parse_retry_after(response.headers.get("Retry-After")),
    )
