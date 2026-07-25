from collections.abc import Mapping
from dataclasses import dataclass, field

from curl_cffi import CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.discord.images import (
    AnhochImageDownloader,
    BrowserImpersonation,
    ContentCallback,
    DownloadedImage,
    ImageResponse,
)

IMAGE_URL = "https://www.anhoch.com/storage/media/product.jpg"


@dataclass(frozen=True, slots=True)
class StubImageResponse:
    chunks: tuple[bytes, ...]
    status_code: int = 200
    headers: Mapping[str, str] = field(
        default_factory=lambda: {"content-type": "image/jpeg"},
    )
    url: str = IMAGE_URL

@dataclass(frozen=True, slots=True)
class RecordingImageSession:
    response: StubImageResponse
    calls: list[tuple[str, str, int, bool, Mapping[CurlOpt, int]]] = field(
        default_factory=list,
    )

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
        self.calls.append((url, impersonate, timeout, allow_redirects, curl_options))
        for chunk in self.response.chunks:
            result = content_callback(chunk)
            if result == CURL_WRITEFUNC_ERROR:
                break
            assert result == len(chunk)
        return self.response


@dataclass(frozen=True, slots=True)
class SequenceImageSession:
    responses: list[StubImageResponse]
    calls: list[str] = field(default_factory=list)

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
        del impersonate, timeout, allow_redirects, curl_options
        self.calls.append(url)
        response = self.responses.pop(0)
        for chunk in response.chunks:
            result = content_callback(chunk)
            if result == CURL_WRITEFUNC_ERROR:
                break
            assert result == len(chunk)
        return response


def test_anhoch_image_downloader_returns_bounded_jpeg() -> None:
    # Given
    session = RecordingImageSession(
        StubImageResponse(chunks=(b"\xff\xd8\xffimage", b"-bytes")),
    )

    # When
    image = AnhochImageDownloader(session).download(IMAGE_URL)

    # Then
    assert image == DownloadedImage(
        filename="product-image.jpg",
        content_type="image/jpeg",
        content=b"\xff\xd8\xffimage-bytes",
    )
    assert session.calls == [
        (IMAGE_URL, "chrome", 30, False, {CurlOpt.TIMEOUT_MS: 30_000}),
    ]


def test_anhoch_image_downloader_rejects_html_response() -> None:
    # Given
    response = StubImageResponse(
        chunks=(b"blocked",),
        headers={"Content-Type": "text/html"},
    )

    # When
    image = AnhochImageDownloader(RecordingImageSession(response)).download(IMAGE_URL)

    # Then
    assert image is None


def test_anhoch_image_downloader_rejects_cross_origin_redirect() -> None:
    # Given
    response = StubImageResponse(
        chunks=(b"image",),
        url="https://example.test/product.jpg",
    )

    # When
    image = AnhochImageDownloader(RecordingImageSession(response)).download(IMAGE_URL)

    # Then
    assert image is None


def test_anhoch_image_downloader_rejects_oversized_content() -> None:
    # Given
    response = StubImageResponse(
        chunks=(b"",),
        headers={"Content-Type": "image/jpeg", "Content-Length": "8388609"},
    )

    # When
    image = AnhochImageDownloader(RecordingImageSession(response)).download(IMAGE_URL)

    # Then
    assert image is None


def test_anhoch_image_downloader_aborts_unknown_length_oversize() -> None:
    # Given
    response = StubImageResponse(chunks=(b"x" * (8 * 1024 * 1024 + 1),))

    # When
    image = AnhochImageDownloader(RecordingImageSession(response)).download(IMAGE_URL)

    # Then
    assert image is None


def test_anhoch_image_downloader_does_not_request_untrusted_url() -> None:
    # Given
    session = RecordingImageSession(StubImageResponse(chunks=(b"image",)))

    # When
    image = AnhochImageDownloader(session).download(
        "https://example.test/storage/media/product.jpg",
    )

    # Then
    assert image is None
    assert session.calls == []


def test_anhoch_image_downloader_follows_same_origin_redirect() -> None:
    # Given
    redirected_url = "https://www.anhoch.com/storage/media/redirected.jpg"
    session = SequenceImageSession(
        responses=[
            StubImageResponse(
                chunks=(),
                status_code=302,
                headers={"Location": "/storage/media/redirected.jpg"},
            ),
            StubImageResponse(chunks=(b"\xff\xd8\xffimage",), url=redirected_url),
        ],
    )

    # When
    image = AnhochImageDownloader(session).download(IMAGE_URL)

    # Then
    assert image is not None
    assert session.calls == [IMAGE_URL, redirected_url]


def test_anhoch_image_downloader_rejects_alternate_port_before_request() -> None:
    # Given
    session = RecordingImageSession(StubImageResponse(chunks=(b"image",)))

    # When
    image = AnhochImageDownloader(session).download(
        "https://www.anhoch.com:444/storage/media/product.jpg",
    )

    # Then
    assert image is None
    assert session.calls == []


def test_anhoch_image_downloader_rejects_encoded_traversal_before_request() -> None:
    # Given
    session = RecordingImageSession(StubImageResponse(chunks=(b"image",)))

    # When
    image = AnhochImageDownloader(session).download(
        "https://www.anhoch.com/storage/media/%2e%2e/admin",
    )

    # Then
    assert image is None
    assert session.calls == []


def test_anhoch_image_downloader_rejects_mismatched_image_signature() -> None:
    # Given
    session = RecordingImageSession(StubImageResponse(chunks=(b"not-a-jpeg",)))

    # When
    image = AnhochImageDownloader(session).download(IMAGE_URL)

    # Then
    assert image is None
