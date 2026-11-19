"""Shared bounded HTTP response reading for provider fetches."""

from collections.abc import Callable, Iterator
from typing import Protocol

from rss2discord.fetch_errors import FeedFetchError


class HeaderSource(Protocol):
    """Minimal header lookup surface shared by requests and curl_cffi."""

    def get(self, name: str) -> str | None: ...


class BoundedHeaders(Protocol):
    """Minimal response surface for content-length validation."""

    @property
    def headers(self) -> HeaderSource: ...


class BoundedResponse(BoundedHeaders, Protocol):
    """Minimal response surface shared by requests and curl_cffi."""

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
    on_chunk: Callable[[int], None] | None = None,
    charge_before_check: bool = False,
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
        if charge_before_check and on_chunk is not None:
            on_chunk(len(chunk))
        cause = _too_large_cause(
            len(content) + len(chunk),
            max_bytes,
            remaining_bytes,
            response_cause,
            scan_cause,
        )
        if cause is not None:
            raise FeedFetchError(label, cause)
        if not charge_before_check and on_chunk is not None:
            on_chunk(len(chunk))
        content.extend(chunk)
    return bytes(content)


def validate_content_length(
    response: BoundedHeaders,
    *,
    label: str,
    max_bytes: int,
    invalid_cause: str | None = None,
) -> None:
    """Reject a declared response body larger than the per-response limit.

    Invalid values fall through to streaming unless ``invalid_cause`` is set,
    which lets providers that treat a malformed header as fatal keep doing so.
    """
    content_length = response.headers.get("Content-Length")
    if content_length is None:
        return
    if invalid_cause is not None:
        if not content_length.isdigit():
            raise FeedFetchError(label, invalid_cause)
        declared_bytes = int(content_length)
    else:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            return
    if declared_bytes > max_bytes:
        raise FeedFetchError(label, "ResponseTooLarge")


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
