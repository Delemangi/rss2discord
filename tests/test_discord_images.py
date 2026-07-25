from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from rss2discord.discord.images import (
    AnhochImageDownloader,
    BrowserImpersonation,
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

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size == 65_536
        yield from self.chunks

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class RecordingImageSession:
    response: StubImageResponse
    calls: list[tuple[str, str, int, bool, bool]] = field(default_factory=list)

    def get(
        self,
        url: str,
        *,
        impersonate: BrowserImpersonation,
        timeout: int,
        allow_redirects: bool,
        stream: bool,
    ) -> ImageResponse:
        self.calls.append((url, impersonate, timeout, allow_redirects, stream))
        return self.response


def test_anhoch_image_downloader_returns_bounded_jpeg() -> None:
    # Given
    session = RecordingImageSession(StubImageResponse(chunks=(b"image", b"-bytes")))

    # When
    image = AnhochImageDownloader(session).download(IMAGE_URL)

    # Then
    assert image == DownloadedImage(
        filename="product-image.jpg",
        content_type="image/jpeg",
        content=b"image-bytes",
    )
    assert session.calls == [(IMAGE_URL, "chrome", 30, False, True)]


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
