"""Shared bounded HTTP response reading tests."""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

import pytest

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.transports.bounded_http import (
    read_bounded_content,
    validate_content_length,
)
from tests.fakes import StubResponse


@dataclass(frozen=True, slots=True)
class ChunkedResponse:
    chunks: tuple[bytes, ...]
    headers: Mapping[str, str] = field(default_factory=dict)

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield from self.chunks


def read(
    content: bytes,
    *,
    max_bytes: int = 10,
    remaining_bytes: int | None = None,
    headers: Mapping[str, str] | None = None,
    response_cause: str = "ResponseTooLarge",
    scan_cause: str = "ScanResponseTooLarge",
    on_chunk: Callable[[int], None] | None = None,
    charge_before_check: bool = False,
) -> bytes:
    return read_bounded_content(
        StubResponse(content, headers=dict(headers or {})),
        label="Test",
        max_bytes=max_bytes,
        chunk_bytes=4,
        remaining_bytes=remaining_bytes,
        response_cause=response_cause,
        scan_cause=scan_cause,
        on_chunk=on_chunk,
        charge_before_check=charge_before_check,
    )


def test_reads_content_within_limits() -> None:
    assert read(b"abc") == b"abc"


def test_reads_content_across_multiple_chunks() -> None:
    response = ChunkedResponse((b"1234", b"5678", b"9"))

    assert (
        read_bounded_content(
            response,
            label="Test",
            max_bytes=9,
            chunk_bytes=4,
        )
        == b"123456789"
    )


def test_rejects_cumulative_oversize_across_chunks() -> None:
    response = ChunkedResponse((b"1234", b"5678", b"90"))

    with pytest.raises(FeedFetchError) as error:
        read_bounded_content(
            response,
            label="Test",
            max_bytes=9,
            chunk_bytes=4,
        )

    assert error.value.cause_type == "ResponseTooLarge"


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


def test_on_chunk_charges_each_streamed_chunk() -> None:
    charges: list[int] = []

    assert read(b"abcd", on_chunk=charges.append) == b"abcd"
    assert charges == [4]


def test_charge_before_check_runs_before_the_streamed_limit() -> None:
    charged: list[int] = []

    with pytest.raises(FeedFetchError):
        read(
            b"x" * 11,
            on_chunk=charged.append,
            charge_before_check=True,
        )

    assert charged == [11]


def test_on_chunk_runs_only_after_a_successful_limit_check() -> None:
    charged: list[int] = []

    with pytest.raises(FeedFetchError):
        read(b"x" * 11, on_chunk=charged.append)

    assert charged == []


def validate(
    content_length: str | None,
    *,
    max_bytes: int = 10,
    invalid_cause: str | None = None,
) -> None:
    headers = {} if content_length is None else {"Content-Length": content_length}
    validate_content_length(
        StubResponse(b"", headers=headers),
        label="Test",
        max_bytes=max_bytes,
        invalid_cause=invalid_cause,
    )


def test_validate_content_length_accepts_within_limit() -> None:
    validate("10")


def test_validate_content_length_rejects_declared_oversize() -> None:
    with pytest.raises(FeedFetchError) as error:
        validate("11")
    assert error.value.cause_type == "ResponseTooLarge"


def test_validate_content_length_ignores_missing_or_invalid_values() -> None:
    validate(None)
    validate("nope")


def test_validate_content_length_can_reject_invalid_values() -> None:
    with pytest.raises(FeedFetchError) as error:
        validate("nope", invalid_cause="ResponseTooLarge")
    assert error.value.cause_type == "ResponseTooLarge"

    with pytest.raises(FeedFetchError) as negative:
        validate("-1", invalid_cause="ResponseTooLarge")
    assert negative.value.cause_type == "ResponseTooLarge"
