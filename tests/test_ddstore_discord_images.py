from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest
import requests
from curl_cffi import CurlOpt

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordDeliveryResult, DiscordWebhookClient
from rss2discord.discord.image_retries import RetrySleep
from rss2discord.discord.image_urls import ImageSource
from rss2discord.discord.images import (
    BrowserImpersonation,
    ContentCallback,
    DownloadedImage,
    ImageResponse,
    ProductImageDownloader,
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
class RecordingAnhochImageDownloader:
    calls: list[str]

    def download(self, url: str) -> None:
        self.calls.append(url)


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
        _ = content_callback(b"\x89PNG\r\n\x1a\nimage-bytes")
        return DDStoreImageResponse(url=self.response_url)


def make_ddstore_message() -> WebhookMessage:
    return WebhookMessage(
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


def test_ddstore_delivery_uploads_thumbnail_instead_of_using_origin_url() -> None:
    # Given
    image = DownloadedImage(
        filename="product-image.png",
        content_type="image/png",
        content=b"\x89PNG\r\n\x1a\nimage-bytes",
    )

    # When
    delivery = prepare_delivery(
        make_ddstore_message(),
        StaticDDStoreImageDownloader(image),
        lambda _: True,
    )

    # Then
    assert delivery.request.image == image
    assert delivery.request.payload["attachments"] == [
        {"id": 0, "filename": "product-image.png"},
    ]
    assert DDSTORE_IMAGE_URL not in str(delivery.request.payload)


def test_ddstore_delivery_does_not_use_injected_anhoch_downloader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    image = DownloadedImage(
        filename="product-image.png",
        content_type="image/png",
        content=b"\x89PNG\r\n\x1a\nimage-bytes",
    )
    anhoch_downloader = RecordingAnhochImageDownloader(calls=[])

    def make_image_downloader(
        *,
        source: ImageSource,
        sleep: RetrySleep,
    ) -> StaticDDStoreImageDownloader:
        del sleep
        assert source == "ddstore"
        return StaticDDStoreImageDownloader(image)

    session = requests.Session()

    def post(url: str, **kwargs: str) -> requests.Response:
        del url, kwargs
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"id":"123"}'
        return response

    monkeypatch.setattr(
        "rss2discord.discord.client.ProductImageDownloader",
        make_image_downloader,
    )
    monkeypatch.setattr(session, "post", post)

    # When
    result = DiscordWebhookClient(
        session,
        image_downloader=anhoch_downloader,
    ).send(make_ddstore_message(), lambda _: True)

    # Then
    assert anhoch_downloader.calls == []
    assert result is DiscordDeliveryResult.DELIVERED


def test_image_downloader_accepts_ddstore_catalog_image() -> None:
    # Given
    session = DDStoreImageSession(calls=[])

    # When
    image = ProductImageDownloader(session, source="ddstore").download(
        DDSTORE_IMAGE_URL,
    )

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
    image = ProductImageDownloader(session, source="ddstore").download(
        DDSTORE_MEDIA_IMAGE_URL,
    )

    # Then
    assert image is not None
    assert session.calls == [DDSTORE_MEDIA_IMAGE_URL]
