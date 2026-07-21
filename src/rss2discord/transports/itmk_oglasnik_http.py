"""Bounded HTTP transport for IT.mk Oglasnik pages."""

import math
from typing import Final
from urllib.parse import urljoin

import requests

from rss2discord.transports.base import FeedFetchError

ITMK_OGLASNIK_LABEL: Final = "IT.mk Oglasnik"
ITMK_OGLASNIK_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
MAX_ITMK_OGLASNIK_PAGE_BYTES: Final = 1_048_576
ITMK_OGLASNIK_STREAM_CHUNK_BYTES: Final = 65_536
MAX_ITMK_OGLASNIK_REDIRECTS: Final = 10


def fetch_itmk_oglasnik_page(url: str) -> tuple[bytes, str]:
    """Fetch one marketplace page while bounding every response body."""
    try:
        return _fetch_page(url)
    except FeedFetchError:
        raise
    except (requests.ConnectionError, requests.Timeout) as error:
        raise FeedFetchError(
            ITMK_OGLASNIK_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None
    except requests.RequestException as error:
        raise FeedFetchError(
            ITMK_OGLASNIK_LABEL,
            type(error).__name__,
        ) from None


def _fetch_page(url: str) -> tuple[bytes, str]:
    current_url = url
    for _ in range(MAX_ITMK_OGLASNIK_REDIRECTS + 1):
        with requests.get(
            current_url,
            headers={"User-Agent": ITMK_OGLASNIK_USER_AGENT},
            timeout=30,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.is_redirect:
                _read_content(response)
                location = response.headers.get("Location")
                if location is None:
                    raise FeedFetchError(
                        ITMK_OGLASNIK_LABEL,
                        "InvalidRedirect",
                    )
                current_url = urljoin(response.url, location)
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError:
                status_code = response.status_code
                raise FeedFetchError(
                    ITMK_OGLASNIK_LABEL,
                    "HTTPError",
                    status_code=status_code,
                    retryable=status_code == 429 or 500 <= status_code < 600,
                    retry_after=_parse_retry_after(
                        response.headers.get("Retry-After"),
                    ),
                ) from None
            return _read_content(response), response.url
    raise FeedFetchError(ITMK_OGLASNIK_LABEL, "TooManyRedirects")


def _read_content(response: requests.Response) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_ITMK_OGLASNIK_PAGE_BYTES:
            raise FeedFetchError(ITMK_OGLASNIK_LABEL, "ResponseTooLarge")

    content = bytearray()
    for chunk in response.iter_content(chunk_size=ITMK_OGLASNIK_STREAM_CHUNK_BYTES):
        if len(content) + len(chunk) > MAX_ITMK_OGLASNIK_PAGE_BYTES:
            raise FeedFetchError(ITMK_OGLASNIK_LABEL, "ResponseTooLarge")
        content.extend(chunk)
    return bytes(content)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except ValueError:
        return None
    return retry_after if math.isfinite(retry_after) and retry_after >= 0 else None
