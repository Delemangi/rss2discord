"""Secure, bounded search retrieval for the Setec catalog."""

from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests

from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_REDIRECTS,
    SETEC_LABEL,
    SETEC_SEARCH_KEY,
    SETEC_SEARCH_URL,
    SETEC_STREAM_CHUNK_BYTES,
    SETEC_USER_AGENT,
    SetecSearchRequest,
)

SEARCH_REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class FetchedSetecSearch:
    """A raw search response body with the bytes consumed for its request chain."""

    content: bytes
    response_bytes: int


class SetecSearchClient:
    """Query the Setec search index without trusting redirects or response sizes."""

    def build_search_url(self, url: str) -> str:
        """Validate a configured catalog URL and return the Setec search endpoint."""
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
        return SETEC_SEARCH_URL

    def search(
        self,
        search_url: str,
        request: SetecSearchRequest,
    ) -> FetchedSetecSearch:
        """Run one search while enforcing redirect and response-byte bounds."""
        body = dict(request.body)
        current_url = search_url
        try:
            response_bytes = 0
            for _ in range(MAX_SETEC_REDIRECTS + 1):
                with requests.post(
                    current_url,
                    json=body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {SETEC_SEARCH_KEY}",
                        "User-Agent": SETEC_USER_AGENT,
                    },
                    timeout=SEARCH_REQUEST_TIMEOUT_SECONDS,
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
                            request,
                            response_bytes=response_bytes,
                        )
                        response_bytes += len(redirect_content)
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
                            retry_after=parse_retry_after(
                                response.headers.get("Retry-After"),
                            ),
                        ) from None
                    response_content = _read_content(
                        response,
                        request,
                        response_bytes=response_bytes,
                    )
                    response_bytes += len(response_content)
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
        return FetchedSetecSearch(
            content=response_content,
            response_bytes=response_bytes,
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
    request: SetecSearchRequest,
    *,
    response_bytes: int,
) -> bytes:
    remaining_global_scan_bytes = request.remaining_scan_response_bytes - response_bytes
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > request.max_single_response_bytes:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
        if declared_bytes > remaining_global_scan_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(chunk_size=SETEC_STREAM_CHUNK_BYTES)
    for chunk in chunks:
        if len(content) + len(chunk) > request.max_single_response_bytes:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
        if len(content) + len(chunk) > remaining_global_scan_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
        content.extend(chunk)
    return bytes(content)
