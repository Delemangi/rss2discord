"""Secure, bounded HTTP retrieval for Setec catalog pages."""

import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests
from pydantic import ValidationError

from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_REDIRECTS,
    SETEC_API_PATH,
    SETEC_LABEL,
    SETEC_REGION_ID,
    SETEC_STREAM_CHUNK_BYTES,
    SETEC_USER_AGENT,
    CatalogPageRequest,
)
from rss2discord.transports.setec_models import SetecCatalogResponse


@dataclass(frozen=True, slots=True)
class FetchedSetecPage:
    """A validated catalog response with bytes consumed for its request chain."""

    catalog_response: SetecCatalogResponse
    page_response_bytes: int


class SetecHttpClient:
    """Retrieve Setec API pages without trusting redirects or response sizes."""

    def build_api_url(self, url: str) -> str:
        """Validate a catalog URL and return its credential-free Setec API endpoint."""
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            username = parsed.username
            password = parsed.password
        except ValueError:
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl") from None
        if (
            parsed.scheme not in {"http", "https"}
            or hostname is None
            or username is not None
            or password is not None
        ):
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl")
        return urlunsplit(parsed._replace(path=SETEC_API_PATH, query="", fragment=""))

    def fetch_page(
        self,
        api_url: str,
        page_request: CatalogPageRequest,
    ) -> FetchedSetecPage:
        """Fetch one API page while enforcing redirect and response-byte bounds."""
        query = urlencode(
            {
                "limit": page_request.limit,
                "offset": page_request.offset,
                "region_id": SETEC_REGION_ID,
            },
        )
        current_url = f"{api_url}?{query}"
        try:
            page_response_bytes = 0
            for _ in range(MAX_SETEC_REDIRECTS + 1):
                with requests.get(
                    current_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": SETEC_USER_AGENT,
                    },
                    timeout=30,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if location is None:
                            raise FeedFetchError(
                                SETEC_LABEL,
                                "InvalidRedirect",
                            ) from None
                        redirect_content = _read_content(
                            response,
                            page_request,
                            page_response_bytes=page_response_bytes,
                        )
                        page_response_bytes += len(redirect_content)
                        current_url = _same_origin_redirect_url(current_url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except requests.HTTPError:
                        status_code = response.status_code
                        raise FeedFetchError(
                            SETEC_LABEL,
                            "HTTPError",
                            status_code=status_code,
                            retryable=status_code in {408, 429}
                            or 500 <= status_code < 600,
                            retry_after=_parse_retry_after(
                                response.headers.get("Retry-After"),
                            ),
                        ) from None
                    response_content = _read_content(
                        response,
                        page_request,
                        page_response_bytes=page_response_bytes,
                    )
                    page_response_bytes += len(response_content)
                    break
            else:
                raise FeedFetchError(SETEC_LABEL, "TooManyRedirects") from None
        except ValueError:
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl") from None
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as error:
            raise FeedFetchError(
                SETEC_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(SETEC_LABEL, type(error).__name__) from None
        try:
            catalog_response = SetecCatalogResponse.model_validate_json(
                response_content,
            )
        except ValidationError:
            raise FeedFetchError(SETEC_LABEL, "InvalidResponse") from None
        return FetchedSetecPage(
            catalog_response=catalog_response,
            page_response_bytes=page_response_bytes,
        )


def _same_origin_redirect_url(current_url: str, location: str) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        current = urlsplit(current_url)
        redirected = urlsplit(redirected_url)
        default_port = 443 if current.scheme == "https" else 80
        current_port = current.port or default_port
        redirected_port = redirected.port or default_port
    except ValueError:
        raise FeedFetchError(SETEC_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != current.scheme
        or redirected.hostname != current.hostname
        or redirected_port != current_port
        or redirected.username is not None
        or redirected.password is not None
    ):
        raise FeedFetchError(SETEC_LABEL, "InvalidRedirect")
    return redirected_url


def _read_content(
    response: requests.Response,
    page_request: CatalogPageRequest,
    *,
    page_response_bytes: int,
) -> bytes:
    remaining_global_scan_bytes = (
        page_request.remaining_scan_response_bytes - page_response_bytes
    )
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > page_request.max_single_response_bytes:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
        if declared_bytes > remaining_global_scan_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(chunk_size=SETEC_STREAM_CHUNK_BYTES)
    for chunk in chunks:
        if len(content) + len(chunk) > page_request.max_single_response_bytes:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
        if len(content) + len(chunk) > remaining_global_scan_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
        content.extend(chunk)
    return bytes(content)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)
    return retry_after if math.isfinite(retry_after) and retry_after >= 0 else None
