"""Shared bounded HTTP response reading tests."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

import pytest

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.transports.bounded_http import read_bounded_content


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield self.content


def read(
    content: bytes,
    *,
    max_bytes: int = 10,
    remaining_bytes: int | None = None,
    headers: Mapping[str, str] | None = None,
    response_cause: str = "ResponseTooLarge",
    scan_cause: str = "ScanResponseTooLarge",
) -> bytes:
    return read_bounded_content(
        StubResponse(content, headers or {}),
        label="Test",
        max_bytes=max_bytes,
        chunk_bytes=4,
        remaining_bytes=remaining_bytes,
        response_cause=response_cause,
        scan_cause=scan_cause,
    )


def test_reads_content_within_limits() -> None:
    assert read(b"abc") == b"abc"


def test_rejects_declared_response_oversize() -> None:
    with pytest.raises(FeedFetchError) as error:
        read(b"", headers={"Content-Length": "11"})
    assert error.value.cause_type == "ResponseTooLarge"


def test_rejects_streamed_response_oversize() -> None:
    with pytest.raises(FeedFetchError) as error:
        read(b"x" * 11)
    assert error.value.cause_type == "ResponseTooLarge"


def test_rejects_declared_scan_oversize() -> None:
    with pytest.raises(FeedFetchError) as error:
        read(b"", max_bytes=10, remaining_bytes=5, headers={"Content-Length": "6"})
    assert error.value.cause_type == "ScanResponseTooLarge"


def test_rejects_streamed_scan_oversize() -> None:
    with pytest.raises(FeedFetchError) as error:
        read(b"x" * 6, max_bytes=10, remaining_bytes=5)
    assert error.value.cause_type == "ScanResponseTooLarge"


def test_uses_response_cause_when_remaining_covers_response() -> None:
    with pytest.raises(FeedFetchError) as error:
        read(b"x" * 11, max_bytes=10, remaining_bytes=20)
    assert error.value.cause_type == "ResponseTooLarge"


def test_invalid_content_length_falls_back_to_streaming() -> None:
    assert read(b"abc", headers={"Content-Length": "nope"}) == b"abc"


def test_streamed_both_limits_uses_response_cause() -> None:
    with pytest.raises(FeedFetchError) as error:
        read(b"x" * 11, max_bytes=10, remaining_bytes=5)
    assert error.value.cause_type == "ResponseTooLarge"


def test_custom_causes_apply_to_declared_and_streamed_limits() -> None:
    with pytest.raises(FeedFetchError) as declared:
        read(
            b"",
            max_bytes=10,
            remaining_bytes=5,
            headers={"Content-Length": "6"},
            response_cause="ScanResponseTooLarge",
            scan_cause="ScanResponseTooLarge",
        )
    assert declared.value.cause_type == "ScanResponseTooLarge"

    with pytest.raises(FeedFetchError) as streamed:
        read(
            b"x" * 6,
            max_bytes=10,
            remaining_bytes=5,
            response_cause="ScanResponseTooLarge",
            scan_cause="ScanResponseTooLarge",
        )
    assert streamed.value.cause_type == "ScanResponseTooLarge"
