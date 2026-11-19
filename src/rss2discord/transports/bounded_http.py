"""Shared bounded HTTP response reading for provider fetches."""

from collections.abc import Iterator, Mapping
from typing import Protocol

from rss2discord.fetch_errors import FeedFetchError


class BoundedResponse(Protocol):
    """Minimal response surface shared by requests and curl_cffi."""

    @property
    def headers(self) -> Mapping[str, str]: ...

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...


def read_bounded_content(
    response: BoundedResponse,
    *,
    label: str,
    max_bytes: int,
    chunk_bytes: int,
    remaining_bytes: int | None = None,
    response_cause: str = "ResponseTooLarge",
    scan_cause: str = "ScanResponseTooLarge",
) -> bytes:
    """Read a response body while enforcing per-response and scan limits."""
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        cause = _too_large_cause(
            declared_bytes,
            max_bytes,
            remaining_bytes,
            response_cause,
            scan_cause,
        )
        if cause is not None:
            raise FeedFetchError(label, cause)

    content = bytearray()
    for chunk in response.iter_content(chunk_size=chunk_bytes):
        cause = _too_large_cause(
            len(content) + len(chunk),
            max_bytes,
            remaining_bytes,
            response_cause,
            scan_cause,
        )
        if cause is not None:
            raise FeedFetchError(label, cause)
        content.extend(chunk)
    return bytes(content)


def _too_large_cause(
    length: int,
    max_bytes: int,
    remaining_bytes: int | None,
    response_cause: str,
    scan_cause: str,
) -> str | None:
    if length > max_bytes:
        return response_cause
    if remaining_bytes is not None and length > remaining_bytes:
        return scan_cause
    return None
