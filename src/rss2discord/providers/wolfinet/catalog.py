"""Bounded HTTP catalog client for the Wolfinet Odoo shop."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

import requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.wolfinet.bounds import (
    MAX_WOLFINET_LATEST_PAGES,
    MAX_WOLFINET_PAGES,
    MAX_WOLFINET_PRODUCTS,
    MAX_WOLFINET_RESPONSE_BYTES,
    MAX_WOLFINET_SCAN_BYTES,
    WOLFINET_HOST,
    WOLFINET_INTER_REQUEST_DELAY,
    WOLFINET_LABEL,
    WOLFINET_ORIGIN,
    WOLFINET_REQUEST_TIMEOUT,
    WOLFINET_SHOP_PATH,
    WOLFINET_USER_AGENT,
)
from rss2discord.providers.wolfinet.models import WolfinetProduct
from rss2discord.providers.wolfinet.parser import parse_wolfinet_page
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    parse_retry_after,
)
from rss2discord.transports.bounded_http import read_bounded_content

__all__ = [
    "WolfinetCatalogClient",
    "page_url",
    "parse_wolfinet_page",
    "validate_wolfinet_url",
]

_STREAM_CHUNK_BYTES: Final = 65_536
_ALLOWED_HOSTS: Final = frozenset({WOLFINET_HOST, f"www.{WOLFINET_HOST}"})
_NEWEST_SORT: Final = "?order=create_date+desc"


def validate_wolfinet_url(url: str) -> str:
    """Validate the credential-free Wolfinet shop root and canonicalize it."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(WOLFINET_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != WOLFINET_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != WOLFINET_SHOP_PATH
    ):
        raise FeedFetchError(WOLFINET_LABEL, "InvalidUrl")
    return WOLFINET_ORIGIN + WOLFINET_SHOP_PATH


def page_url(base_url: str, page_number: int) -> str:
    """Build the provider-owned newest-first pagination URL for one page."""
    suffix = "" if page_number == 1 else f"/page/{page_number}"
    return f"{base_url}{suffix}{_NEWEST_SORT}"


class WolfinetCatalogClient:
    """Enumerate bounded Wolfinet shop pages in newest-first order."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep

    def fetch_latest(
        self,
        url: str,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[WolfinetProduct, ...]:
        """Fetch up to two newest-first pages and deduplicate their products."""
        base_url = validate_wolfinet_url(url)
        products: list[WolfinetProduct] = []
        seen: set[str] = set()
        consumed_bytes = 0
        for page_number in range(1, MAX_WOLFINET_LATEST_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if page_number > 1:
                self._sleep(WOLFINET_INTER_REQUEST_DELAY)
            page_products, page_bytes = self._fetch_page(
                page_url(base_url, page_number),
                remaining_bytes=MAX_WOLFINET_SCAN_BYTES - consumed_bytes,
            )
            consumed_bytes += page_bytes
            if not page_products:
                break
            _append_unique(products, seen, page_products)
            if len(products) > MAX_WOLFINET_PRODUCTS:
                raise FeedFetchError(WOLFINET_LABEL, "ProductLimitExceeded")
        return tuple(products)

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[WolfinetProduct, ...]:
        """Fetch every shop page newest-first until empty or bounded."""
        base_url = validate_wolfinet_url(url)

        def scan() -> tuple[WolfinetProduct, ...]:
            return self._scan_catalog(base_url, is_shutdown_requested)

        if retry_policy is None:
            return scan()
        return retry_policy.execute(scan)

    def _scan_catalog(
        self,
        base_url: str,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[WolfinetProduct, ...]:
        products: list[WolfinetProduct] = []
        seen: set[str] = set()
        consumed_bytes = 0
        for page_number in range(1, MAX_WOLFINET_PAGES + 1):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            if page_number > 1:
                self._sleep(WOLFINET_INTER_REQUEST_DELAY)
            page_products, page_bytes = self._fetch_page(
                page_url(base_url, page_number),
                remaining_bytes=MAX_WOLFINET_SCAN_BYTES - consumed_bytes,
            )
            consumed_bytes += page_bytes
            _append_unique(products, seen, page_products)
            if len(products) > MAX_WOLFINET_PRODUCTS:
                raise FeedFetchError(WOLFINET_LABEL, "ProductLimitExceeded")
            if not page_products:
                break
        return tuple(products)

    def _fetch_page(
        self,
        url: str,
        *,
        remaining_bytes: int,
    ) -> tuple[tuple[WolfinetProduct, ...], int]:
        html = self._fetch_html(url, remaining_bytes=remaining_bytes)
        return parse_wolfinet_page(html), len(html.encode("utf-8"))

    @staticmethod
    def _fetch_html(url: str, *, remaining_bytes: int) -> str:
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "text/html",
                    "User-Agent": WOLFINET_USER_AGENT,
                },
                timeout=WOLFINET_REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
        except requests.Timeout as error:
            raise FeedFetchError(
                WOLFINET_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.ConnectionError as error:
            raise FeedFetchError(
                WOLFINET_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.TooManyRedirects as error:
            raise FeedFetchError(
                WOLFINET_LABEL,
                type(error).__name__,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(
                WOLFINET_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except ValueError:
            raise FeedFetchError(WOLFINET_LABEL, "InvalidUrl") from None
        try:
            _validate_final_url(getattr(response, "url", url))
            _check_response_status(response)
            content = read_bounded_content(
                response,
                label=WOLFINET_LABEL,
                max_bytes=MAX_WOLFINET_RESPONSE_BYTES,
                chunk_bytes=_STREAM_CHUNK_BYTES,
                remaining_bytes=remaining_bytes,
            )
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    return content.decode(response.encoding or "utf-8")
                except (LookupError, UnicodeDecodeError):
                    raise FeedFetchError(WOLFINET_LABEL, "InvalidResponse") from None
        finally:
            response.close()


def _append_unique(
    products: list[WolfinetProduct],
    seen: set[str],
    page_products: tuple[WolfinetProduct, ...],
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
        raise FeedFetchError(WOLFINET_LABEL, "InvalidRedirect") from None
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise FeedFetchError(WOLFINET_LABEL, "InvalidRedirect")


def _check_response_status(response: requests.Response) -> None:
    status_code = response.status_code
    if 300 <= status_code < 400:
        raise FeedFetchError(WOLFINET_LABEL, "InvalidRedirect")
    if status_code < 400:
        return
    raise FeedFetchError(
        WOLFINET_LABEL,
        "HTTPError",
        status_code=status_code,
        retryable=status_code == 429 or 500 <= status_code < 600,
        retry_after=parse_retry_after(response.headers.get("Retry-After")),
    )
