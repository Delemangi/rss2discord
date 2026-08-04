from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import assert_never, final

import pytest
from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlECode
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.discord.images import (
    BrowserImpersonation,
    ContentCallback,
    ImageResponse,
    ProductImageDownloader,
)

IMAGE_URL = "https://www.anhoch.com/storage/media/product.jpg"


@dataclass(frozen=True, slots=True)
class StubImageResponse:
    chunks: tuple[bytes, ...] = ()
    status_code: int = 200
    headers: Mapping[str, str] = field(
        default_factory=lambda: {"content-type": "image/jpeg"},
    )
    url: str = IMAGE_URL
    elapsed_seconds: float = 0.0


@final
class FakeClock:
    """Advance deterministic time through retry waits."""

    def __init__(self, *, continue_sleep: bool = True) -> None:
        self.now = 0.0
        self.continue_sleep = continue_sleep
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> bool:
        self.delays.append(seconds)
        self.now += seconds
        return self.continue_sleep

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True, slots=True)
class SequenceImageSession:
    outcomes: list[StubImageResponse | curl_requests.RequestsError]
    clock: FakeClock | None = None
    calls: list[tuple[str, Mapping[CurlOpt, int]]] = field(default_factory=list)

    def get(
        self,
        url: str,
        *,
        impersonate: BrowserImpersonation,
        timeout: int,
        allow_redirects: bool,
        content_callback: ContentCallback,
        curl_options: Mapping[CurlOpt, int],
    ) -> ImageResponse:
        del impersonate, timeout, allow_redirects
        self.calls.append((url, curl_options))
        outcome = self.outcomes.pop(0)
        match outcome:
            case curl_requests.RequestsError():
                raise outcome
            case StubImageResponse() as response:
                for chunk in response.chunks:
                    if content_callback(chunk) == CURL_WRITEFUNC_ERROR:
                        raise curl_requests.RequestsError("write aborted")
                if self.clock is not None:
                    self.clock.advance(response.elapsed_seconds)
                return response
            case unreachable:
                assert_never(unreachable)


def test_anhoch_image_download_retries_transient_server_error() -> None:
    # Given
    clock = FakeClock()
    session = SequenceImageSession(
        outcomes=[
            StubImageResponse(status_code=503),
            StubImageResponse(chunks=(b"\xff\xd8\xffimage",)),
        ],
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is not None
    assert clock.delays == [1.0]
    assert [url for url, _ in session.calls] == [IMAGE_URL, IMAGE_URL]
    assert session.calls[1][1] == {CurlOpt.TIMEOUT_MS: 29_000}


def test_anhoch_image_download_retries_transient_request_error() -> None:
    # Given
    clock = FakeClock()
    session = SequenceImageSession(
        outcomes=[
            curl_requests.RequestsError("connection reset", CurlECode.RECV_ERROR),
            StubImageResponse(chunks=(b"\xff\xd8\xffimage",)),
        ],
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is not None
    assert clock.delays == [1.0]
    assert len(session.calls) == 2


@pytest.mark.parametrize("status_code", [408, 429, 503])
def test_anhoch_image_download_honors_retry_after_for_transient_response(
    status_code: int,
) -> None:
    # Given
    clock = FakeClock()
    session = SequenceImageSession(
        outcomes=[
            StubImageResponse(status_code=status_code, headers={"Retry-After": "2.5"}),
            StubImageResponse(chunks=(b"\xff\xd8\xffimage",)),
        ],
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is not None
    assert clock.delays == [2.5]


def test_anhoch_image_download_rejects_retry_beyond_total_deadline() -> None:
    # Given
    clock = FakeClock()
    session = SequenceImageSession(
        outcomes=[StubImageResponse(status_code=429, headers={"Retry-After": "30"})],
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is None
    assert clock.delays == []
    assert len(session.calls) == 1


def test_anhoch_image_download_does_not_retry_size_abort() -> None:
    # Given
    clock = FakeClock()
    session = SequenceImageSession(
        outcomes=[StubImageResponse(chunks=(b"x" * (8 * 1024 * 1024 + 1),))],
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is None
    assert clock.delays == []
    assert len(session.calls) == 1


def test_anhoch_image_download_rejects_response_after_total_deadline() -> None:
    # Given
    clock = FakeClock()
    session = SequenceImageSession(
        outcomes=[
            StubImageResponse(
                chunks=(b"\xff\xd8\xffimage",),
                elapsed_seconds=30.001,
            ),
        ],
        clock=clock,
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is None
    assert len(session.calls) == 1


def test_anhoch_image_download_shares_retry_limit_across_redirect() -> None:
    # Given
    clock = FakeClock()
    redirected_url = "https://www.anhoch.com/storage/media/redirected.jpg"
    session = SequenceImageSession(
        outcomes=[
            StubImageResponse(status_code=503),
            StubImageResponse(
                status_code=302,
                headers={"Location": "/storage/media/redirected.jpg"},
            ),
            StubImageResponse(status_code=503, url=redirected_url),
            StubImageResponse(status_code=503, url=redirected_url),
        ],
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is None
    assert clock.delays == [1.0, 2.0]
    assert [url for url, _ in session.calls] == [
        IMAGE_URL,
        IMAGE_URL,
        redirected_url,
        redirected_url,
    ]


def test_anhoch_image_download_does_not_retry_permanent_curl_error() -> None:
    # Given
    clock = FakeClock()
    session = SequenceImageSession(
        outcomes=[
            curl_requests.RequestsError(
                "certificate rejected",
                CurlECode.PEER_FAILED_VERIFICATION,
            ),
        ],
    )

    # When
    image = ProductImageDownloader(
        session,
        sleep=clock.sleep,
        monotonic_clock=clock.monotonic,
    ).download(IMAGE_URL, "anhoch")

    # Then
    assert image is None
    assert clock.delays == []
    assert len(session.calls) == 1
