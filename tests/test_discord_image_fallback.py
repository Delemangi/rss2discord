import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest
import requests
from curl_cffi import CurlOpt

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordDeliveryResult, DiscordWebhookClient
from rss2discord.discord.image_retries import RetrySleep
from rss2discord.discord.images import (
    AnhochImageDownloader,
    BrowserImpersonation,
    ContentCallback,
    DownloadedImage,
    ImageResponse,
)
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData

IMAGE_URL = "https://www.anhoch.com/storage/media/product.jpg"


@dataclass(frozen=True, slots=True)
class StaticImageDownloader:
    image: DownloadedImage | None

    def download(self, url: str) -> DownloadedImage | None:
        assert url == IMAGE_URL
        return self.image


@dataclass(frozen=True, slots=True)
class TransientImageResponse:
    status_code: int = 503
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = IMAGE_URL


@dataclass(frozen=True, slots=True)
class TransientImageSession:
    calls: list[str]

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
        del impersonate, timeout, allow_redirects, content_callback, curl_options
        self.calls.append(url)
        return TransientImageResponse(headers={})


def make_anhoch_message() -> WebhookMessage:
    return WebhookMessage(
        feed=FeedConfig(
            id="anhoch",
            name="Anhoch",
            url="https://www.anhoch.com/products",
            webhook="https://discord.test/api/webhooks/id/token",
            strategy="anhoch",
        ),
        entry=EntryData(
            title="Product",
            link="https://www.anhoch.com/products/product",
            description="",
            author="",
            timestamp=None,
            image_url=IMAGE_URL,
        ),
        source_title="Anhoch",
    )


def successful_response() -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = "https://discord.test/api/webhooks/id/token"
    response._content = b'{"id":"123"}'
    return response


def test_unavailable_thumbnail_logs_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    session = requests.Session()

    def post(url: str, **kwargs: str) -> requests.Response:
        del url, kwargs
        return successful_response()

    monkeypatch.setattr(session, "post", post)
    caplog.set_level(logging.WARNING)

    # When
    delivered = DiscordWebhookClient(
        session,
        image_downloader=StaticImageDownloader(None),
    ).send(make_anhoch_message(), lambda _: True)

    # Then
    assert delivered
    assert (
        caplog.records[-1].getMessage()
        == "Anhoch thumbnail unavailable for feed anhoch"
    )


def test_default_image_downloader_receives_delivery_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    captured_sleep: list[RetrySleep] = []

    def delivery_sleep(seconds: float) -> bool:
        del seconds
        return False

    def post(url: str, **kwargs: str) -> requests.Response:
        del url, kwargs
        return successful_response()

    def make_image_downloader(*, sleep: RetrySleep) -> StaticImageDownloader:
        captured_sleep.append(sleep)
        return StaticImageDownloader(None)

    monkeypatch.setattr(session, "post", post)
    monkeypatch.setattr(
        "rss2discord.discord.client.AnhochImageDownloader",
        make_image_downloader,
    )

    # When
    delivered = DiscordWebhookClient(session).send(
        make_anhoch_message(),
        delivery_sleep,
    )

    # Then
    assert delivered
    assert captured_sleep == [delivery_sleep]


def test_interrupted_image_retry_stops_discord_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    posts: list[str] = []
    image_session = TransientImageSession(calls=[])

    def interrupt_retry(seconds: float) -> bool:
        del seconds
        return False

    def post(url: str, **kwargs: str) -> requests.Response:
        del kwargs
        posts.append(url)
        return successful_response()

    def make_image_downloader(*, sleep: RetrySleep) -> AnhochImageDownloader:
        return AnhochImageDownloader(image_session, sleep=sleep)

    monkeypatch.setattr(session, "post", post)
    monkeypatch.setattr(
        "rss2discord.discord.client.AnhochImageDownloader",
        make_image_downloader,
    )

    # When
    result = DiscordWebhookClient(session).send(
        make_anhoch_message(),
        interrupt_retry,
    )

    # Then
    assert result is DiscordDeliveryResult.INTERRUPTED
    assert image_session.calls == [IMAGE_URL]
    assert posts == []
