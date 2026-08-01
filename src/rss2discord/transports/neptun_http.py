"""Bounded first-party HTTP boundary for Neptun category requests."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from pydantic import JsonValue, ValidationError

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.neptun_models import (
    NeptunInitialSearchModel,
    NeptunProductsResponse,
)

NEPTUN_LABEL: Final = "Neptun"
NEPTUN_ORIGIN: Final = "https://www.neptun.mk"
NEPTUN_PRODUCTS_URL: Final = f"{NEPTUN_ORIGIN}/NeptunCategories/LoadProductsForCategory"
NEPTUN_RESPONSE_BYTES: Final = 5 * 1024 * 1024
NEPTUN_STREAM_CHUNK_BYTES: Final = 64 * 1024
MAX_NEPTUN_REDIRECTS: Final = 5
NEPTUN_USER_AGENT: Final = "Mozilla/5.0 (compatible; rss2discord/0.1)"


@dataclass(frozen=True, slots=True)
class NeptunPageRequest:
    page: int
    page_size: int
    sort: int
    remaining_scan_bytes: int


@dataclass(frozen=True, slots=True)
class FetchedNeptunProducts:
    response: NeptunProductsResponse
    response_bytes: int


class NeptunHttpClient:
    """Retrieve category metadata and product pages without unsafe redirects."""

    def normalize_category_url(self, url: str) -> str:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise FeedFetchError(NEPTUN_LABEL, "InvalidUrl") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.neptun.mk", "neptun.mk"}
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path
        ):
            raise FeedFetchError(NEPTUN_LABEL, "InvalidUrl")
        return urlunsplit(("https", "www.neptun.mk", parsed.path, parsed.query, ""))

    def fetch_category_model(
        self,
        category_url: str,
        *,
        remaining_scan_bytes: int = NEPTUN_RESPONSE_BYTES,
    ) -> tuple[NeptunInitialSearchModel, int]:
        current_url = self.normalize_category_url(category_url)
        response_content, response_bytes = self._request(
            "GET",
            current_url,
            category_url=current_url,
            payload=None,
            remaining_scan_bytes=remaining_scan_bytes,
        )
        soup = BeautifulSoup(response_content, "html.parser")
        candidates = soup.select("#angularApp[data-initialsearchmodel]")
        if len(candidates) != 1:
            raise FeedFetchError(NEPTUN_LABEL, "InvalidCategoryModel")
        encoded_model = candidates[0].get("data-initialsearchmodel")
        if not isinstance(encoded_model, str):
            raise FeedFetchError(NEPTUN_LABEL, "InvalidCategoryModel")
        try:
            model = NeptunInitialSearchModel.model_validate_json(encoded_model)
        except ValidationError:
            raise FeedFetchError(NEPTUN_LABEL, "InvalidCategoryModel") from None
        return model, response_bytes

    def fetch_products(
        self,
        *,
        category_url: str,
        category_id: int,
        request: NeptunPageRequest,
    ) -> FetchedNeptunProducts:
        payload: Mapping[str, JsonValue] = {
            "model": {
                "TotalItems": 0,
                "CurrentPage": request.page,
                "ItemsPerPage": request.page_size,
                "Sort": request.sort,
                "CategoryId": category_id,
                "Recomended": False,
                "ShowAllProducts": True,
            },
        }
        response_content, response_bytes = self._request(
            "POST",
            NEPTUN_PRODUCTS_URL,
            category_url=self.normalize_category_url(category_url),
            payload=payload,
            remaining_scan_bytes=request.remaining_scan_bytes,
        )
        try:
            response = NeptunProductsResponse.model_validate_json(response_content)
        except ValidationError:
            raise FeedFetchError(NEPTUN_LABEL, "InvalidResponse") from None
        return FetchedNeptunProducts(response, response_bytes)

    def _request(
        self,
        method: str,
        url: str,
        *,
        category_url: str,
        payload: Mapping[str, JsonValue] | None,
        remaining_scan_bytes: int,
    ) -> tuple[bytes, int]:
        current_url = url
        consumed_bytes = 0
        try:
            for _ in range(MAX_NEPTUN_REDIRECTS + 1):
                headers = {
                    "Accept": "application/json" if method == "POST" else "text/html",
                    "Referer": category_url,
                    "User-Agent": NEPTUN_USER_AGENT,
                }
                if method == "POST":
                    headers["Content-Type"] = "application/json;charset=UTF-8"
                    response_context = requests.post(
                        current_url,
                        json=payload,
                        headers=headers,
                        timeout=30,
                        stream=True,
                        allow_redirects=False,
                    )
                else:
                    response_context = requests.get(
                        current_url,
                        headers=headers,
                        timeout=30,
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
                            raise FeedFetchError(NEPTUN_LABEL, "InvalidRedirect")
                        current_url = _same_origin_redirect(current_url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except requests.HTTPError:
                        status = response.status_code
                        raise FeedFetchError(
                            NEPTUN_LABEL,
                            "HTTPError",
                            status_code=status,
                            retryable=status in {408, 429} or 500 <= status < 600,
                            retry_after=parse_retry_after(
                                response.headers.get("Retry-After"),
                            ),
                        ) from None
                    return content, consumed_bytes
            raise FeedFetchError(NEPTUN_LABEL, "TooManyRedirects")
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as error:
            raise FeedFetchError(
                NEPTUN_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(NEPTUN_LABEL, type(error).__name__) from None


def _same_origin_redirect(current_url: str, location: str) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        redirected = urlsplit(redirected_url)
        port = redirected.port
    except ValueError:
        raise FeedFetchError(NEPTUN_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != "https"
        or redirected.hostname != "www.neptun.mk"
        or port is not None
        or redirected.username is not None
        or redirected.password is not None
        or redirected.fragment
    ):
        raise FeedFetchError(NEPTUN_LABEL, "InvalidRedirect")
    return redirected_url


def _read_content(
    response: requests.Response,
    *,
    remaining_scan_bytes: int,
) -> bytes:
    response_limit = min(NEPTUN_RESPONSE_BYTES, remaining_scan_bytes)
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > NEPTUN_RESPONSE_BYTES:
            raise FeedFetchError(NEPTUN_LABEL, "ResponseTooLarge")
        if declared_bytes > remaining_scan_bytes:
            raise FeedFetchError(NEPTUN_LABEL, "ScanResponseTooLarge")
    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(NEPTUN_STREAM_CHUNK_BYTES)
    for chunk in chunks:
        if len(content) + len(chunk) > response_limit:
            cause = (
                "ResponseTooLarge"
                if remaining_scan_bytes >= NEPTUN_RESPONSE_BYTES
                else "ScanResponseTooLarge"
            )
            raise FeedFetchError(NEPTUN_LABEL, cause)
        content.extend(chunk)
    return bytes(content)
