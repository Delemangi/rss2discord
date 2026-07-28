"""Bounded, same-origin GraphQL retrieval for DDStore catalog pages."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from pydantic import JsonValue, ValidationError

from rss2discord.retries import FeedFetchInterruptedError, parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.ddstore_budget import DDSTORE_LABEL, DDStoreScanBudget
from rss2discord.transports.ddstore_models import DDStoreCatalogResponse
from rss2discord.transports.ddstore_session import (
    DDStoreHttpResponse,
    DDStoreHttpSession,
    create_ddstore_session,
)

DDSTORE_ORIGIN: Final = "https://ddstore.mk"
DDSTORE_GRAPHQL_URL: Final = f"{DDSTORE_ORIGIN}/graphql"
DDSTORE_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
DDSTORE_PAGE_SIZE: Final = 500
MAX_DDSTORE_REDIRECTS: Final = 5
MAX_DDSTORE_TRANSFER_SECONDS: Final = 10.0

_GRAPHQL_QUERY: Final = """
query Products($search: String!, $pageSize: Int!, $currentPage: Int!, $sort: ProductAttributeSortInput!) {
  products(search: $search, pageSize: $pageSize, currentPage: $currentPage, sort: $sort) {
    total_count
    items {
      uid name url_key url_suffix created_at stock_status
      small_image { url }
      categories { name }
      price_range { minimum_price { final_price { value currency } regular_price { value currency } } }
    }
    page_info { current_page page_size total_pages }
  }
}
"""


@dataclass(frozen=True, slots=True)
class CatalogPageRequest:
    """A GraphQL page request with bounded response budgets."""

    current_page: int
    max_single_response_bytes: int


@dataclass(frozen=True, slots=True)
class FetchedDDStorePage:
    """A validated GraphQL page with bytes consumed by its redirect chain."""

    catalog_response: DDStoreCatalogResponse
    page_response_bytes: int


class _BoundedContent:
    """Accumulate one transfer while recording the exact local abort cause."""

    def __init__(
        self,
        page_request: CatalogPageRequest,
        budget: DDStoreScanBudget,
    ) -> None:
        self.content = bytearray()
        self.page_request = page_request
        self.budget = budget
        self.abort_error: FeedFetchError | FeedFetchInterruptedError | None = None

    def write(self, chunk: bytes) -> int:
        try:
            if (
                len(self.content) + len(chunk)
                > self.page_request.max_single_response_bytes
            ):
                self.abort_error = FeedFetchError(DDSTORE_LABEL, "ResponseTooLarge")
                return CURL_WRITEFUNC_ERROR
            self.budget.consume_bytes(len(chunk))
        except (FeedFetchError, FeedFetchInterruptedError) as error:
            self.abort_error = error
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)


def _create_session() -> DDStoreHttpSession:
    return create_ddstore_session()


class DDStoreHttpClient:
    """POST DDStore GraphQL pages without ambient credentials or proxy state."""

    def __init__(self, session: DDStoreHttpSession | None = None) -> None:
        self._session: DDStoreHttpSession = (
            _create_session() if session is None else session
        )

    def close(self) -> None:
        """Release the dedicated session after one catalog operation."""
        self._session.close()

    def build_graphql_url(self, url: str) -> str:
        """Validate the configured DDStore origin and return its GraphQL endpoint."""
        try:
            parsed = urlsplit(url)
            port = parsed.port or 443
        except ValueError:
            raise FeedFetchError(DDSTORE_LABEL, "InvalidUrl") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "ddstore.mk"
            or port != 443
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise FeedFetchError(DDSTORE_LABEL, "InvalidUrl")
        return DDSTORE_GRAPHQL_URL

    def fetch_page(
        self,
        graphql_url: str,
        page_request: CatalogPageRequest,
        budget: DDStoreScanBudget,
    ) -> FetchedDDStorePage:
        """Fetch one page while enforcing the scan budget through every callback."""
        payload: Mapping[str, JsonValue] = {
            "query": _GRAPHQL_QUERY,
            "variables": {
                "search": "",
                "pageSize": DDSTORE_PAGE_SIZE,
                "currentPage": page_request.current_page,
                "sort": {"name": "ASC"},
            },
        }
        current_url = graphql_url
        response_bytes = 0
        content = _BoundedContent(page_request, budget)
        try:
            for _ in range(MAX_DDSTORE_REDIRECTS + 1):
                content = _BoundedContent(page_request, budget)
                response = self._session.post(
                    current_url,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": DDSTORE_USER_AGENT,
                    },
                    allow_redirects=False,
                    content_callback=content.write,
                    timeout_ms=max(
                        1,
                        math.ceil(
                            min(
                                budget.remaining_seconds(),
                                MAX_DDSTORE_TRANSFER_SECONDS,
                            )
                            * 1000,
                        ),
                    ),
                )
                if content.abort_error is not None:
                    raise content.abort_error
                response_content = bytes(content.content)
                _validate_declared_content_length(response, page_request)
                response_bytes += len(response_content)
                if 300 <= response.status_code < 400:
                    location = _header(response.headers, "location")
                    current_url = _redirect_target(current_url, location)
                    continue
                if response.status_code >= 400:
                    raise _http_error(response)
                break
            else:
                raise _too_many_redirects()
        except (FeedFetchError, FeedFetchInterruptedError):
            raise
        except ValueError:
            raise FeedFetchError(DDSTORE_LABEL, "InvalidUrl") from None
        except requests.RequestsError as error:
            if content.abort_error is not None:
                raise content.abort_error from None
            if budget.is_shutdown_requested():
                raise FeedFetchInterruptedError from None
            budget.remaining_seconds()
            raise FeedFetchError(
                DDSTORE_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        try:
            catalog_response = DDStoreCatalogResponse.model_validate_json(
                response_content,
            )
        except ValidationError:
            raise FeedFetchError(DDSTORE_LABEL, "InvalidResponse") from None
        return FetchedDDStorePage(catalog_response, response_bytes)


def _validate_declared_content_length(
    response: DDStoreHttpResponse,
    page_request: CatalogPageRequest,
) -> None:
    content_length = _header(response.headers, "content-length")
    if content_length is None:
        return
    try:
        declared_bytes = int(content_length)
    except ValueError:
        return
    if declared_bytes > page_request.max_single_response_bytes:
        raise FeedFetchError(DDSTORE_LABEL, "ResponseTooLarge")


def _same_origin_redirect_url(current_url: str, location: str) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        redirected = urlsplit(redirected_url)
        port = redirected.port or 443
    except ValueError:
        raise FeedFetchError(DDSTORE_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != "https"
        or redirected.hostname != "ddstore.mk"
        or port != 443
        or redirected.username is not None
        or redirected.password is not None
    ):
        raise FeedFetchError(DDSTORE_LABEL, "InvalidRedirect")
    return redirected_url


def _redirect_target(current_url: str, location: str | None) -> str:
    if location is None:
        raise FeedFetchError(DDSTORE_LABEL, "InvalidRedirect")
    return _same_origin_redirect_url(current_url, location)


def _http_error(response: DDStoreHttpResponse) -> FeedFetchError:
    return FeedFetchError(
        DDSTORE_LABEL,
        "HTTPError",
        status_code=response.status_code,
        retryable=response.status_code in {408, 429}
        or 500 <= response.status_code < 600,
        retry_after=parse_retry_after(_header(response.headers, "retry-after")),
    )


def _too_many_redirects() -> FeedFetchError:
    return FeedFetchError(DDSTORE_LABEL, "TooManyRedirects")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded_name = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == folded_name),
        None,
    )
