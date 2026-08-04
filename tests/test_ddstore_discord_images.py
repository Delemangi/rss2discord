from collections.abc import Mapping
from dataclasses import dataclass, field

from curl_cffi import CurlOpt

from rss2discord.configuration import FeedConfig
from rss2discord.discord.images import (
    AnhochImageDownloader,
    BrowserImpersonation,
    ContentCallback,
    DownloadedImage,
    ImageResponse,
)
from rss2discord.discord.message import WebhookMessage, prepare_delivery
from rss2discord.models import EntryData

DDSTORE_IMAGE_URL = (
    "https://ddstore.mk/media/catalog/product/cache/74c1057f7991b4edb2bc7bdaa94de933/"
    "9/8/984968_29_w.png"
)
DDSTORE_MEDIA_IMAGE_URL = "https://ddstore.mk/media/1.webp"


@dataclass(frozen=True, slots=True)
class StaticDDStoreImageDownloader:
    image: DownloadedImage

    def download(self, url: str) -> DownloadedImage:
        assert url == DDSTORE_IMAGE_URL
        return self.image


@dataclass(frozen=True, slots=True)
class DDStoreImageResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(
        default_factory=lambda: {"content-type": "image/png"},
    )
    url: str = DDSTORE_IMAGE_URL


@dataclass(frozen=True, slots=True)
class DDStoreImageSession:
    calls: list[str]
    response_url: str = DDSTORE_IMAGE_URL

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
        content_callback(b"\x89PNG\r\n\x1a\nimage-bytes")
        return DDStoreImageResponse(url=self.response_url)


def test_ddstore_delivery_uploads_thumbnail_instead_of_using_origin_url() -> None:
    # Given
    image = DownloadedImage(
        filename="product-image.png",
        content_type="image/png",
        content=b"\x89PNG\r\n\x1a\nimage-bytes",
    )
    message = WebhookMessage(
        feed=FeedConfig(
            id="ddstore",
            name="DDStore",
            url="https://ddstore.mk/",
            webhook="https://discord.test/api/webhooks/id/token",
            strategy="ddstore",
        ),
        entry=EntryData(
            title="Product",
            link="https://ddstore.mk/product.html",
            description="",
            author="",
            timestamp=None,
            image_url=DDSTORE_IMAGE_URL,
        ),
        source_title="DDStore",
    )

    # When
    delivery = prepare_delivery(
        message,
        StaticDDStoreImageDownloader(image),
        lambda _: True,
    )

    # Then
    assert delivery.request.image == image
    assert delivery.request.payload["attachments"] == [
        {"id": 0, "filename": "product-image.png"},
    ]
    assert DDSTORE_IMAGE_URL not in str(delivery.request.payload)


def test_image_downloader_accepts_ddstore_catalog_image() -> None:
    # Given
    session = DDStoreImageSession(calls=[])

    # When
    image = AnhochImageDownloader(session).download(DDSTORE_IMAGE_URL)

    # Then
    assert image == DownloadedImage(
        filename="product-image.png",
        content_type="image/png",
        content=b"\x89PNG\r\n\x1a\nimage-bytes",
    )
    assert session.calls == [DDSTORE_IMAGE_URL]


def test_image_downloader_accepts_existing_ddstore_media_path() -> None:
    # Given
    session = DDStoreImageSession(calls=[], response_url=DDSTORE_MEDIA_IMAGE_URL)

    # When
    image = AnhochImageDownloader(session).download(DDSTORE_MEDIA_IMAGE_URL)

    # Then
    assert image is not None
    assert session.calls == [DDSTORE_MEDIA_IMAGE_URL]
