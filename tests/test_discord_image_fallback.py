import logging
from dataclasses import dataclass

import pytest
import requests

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordDeliveryResult, DiscordWebhookClient
from rss2discord.discord.image_retries import (
    ImageDownloadInterruptedError,
    RetrySleep,
)
from rss2discord.discord.images import DownloadedImage
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
class InterruptedImageDownloader:
    def download(self, url: str) -> DownloadedImage | None:
        assert url == IMAGE_URL
        raise ImageDownloadInterruptedError


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

    def post(url: str, **kwargs: str) -> requests.Response:
        del kwargs
        posts.append(url)
        return successful_response()

    monkeypatch.setattr(session, "post", post)

    # When
    result = DiscordWebhookClient(
        session,
        image_downloader=InterruptedImageDownloader(),
    ).send(make_anhoch_message(), lambda _: True)

    # Then
    assert result is DiscordDeliveryResult.INTERRUPTED
    assert posts == []
