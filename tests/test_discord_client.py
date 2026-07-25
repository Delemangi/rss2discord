import json
import logging
from dataclasses import dataclass

import pytest
import requests

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordWebhookClient, JSONValue, WebhookMessage
from rss2discord.discord.images import DownloadedImage
from rss2discord.models import EntryData

type FilePart = tuple[str, bytes, str]
type PostArgument = int | dict[str, str] | dict[str, JSONValue] | dict[str, FilePart]


@dataclass(frozen=True, slots=True)
class StaticImageDownloader:
    image: DownloadedImage | None

    def download(self, url: str) -> DownloadedImage | None:
        assert url == "https://www.anhoch.com/storage/media/product.jpg"
        return self.image


def make_message(
    webhook: str = "https://discord.test/api/webhooks/id/token",
    embed_color: int | None = None,
) -> WebhookMessage:
    return WebhookMessage(
        feed=FeedConfig(
            id="news",
            name="News",
            url="https://example.test/feed.xml",
            webhook=webhook,
            embed_color=embed_color,
        ),
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="Author",
            timestamp=None,
        ),
        source_title="News",
    )


def make_response(
    status_code: int,
    retry_after: str | None = None,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://discord.test/api/webhooks/id/token"
    if status_code == 200:
        response._content = b'{"id":"123"}'
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


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
            image_url="https://www.anhoch.com/storage/media/product.jpg",
        ),
        source_title="Anhoch",
    )


def test_delivery_uploads_anhoch_image_as_thumbnail_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    arguments: dict[str, PostArgument] = {}

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        del url
        arguments.update(kwargs)
        return make_response(200)

    monkeypatch.setattr(session, "post", post)
    image = DownloadedImage(
        filename="product-image.jpg",
        content_type="image/jpeg",
        content=b"image-bytes",
    )

    # When
    delivered = DiscordWebhookClient(
        session,
        image_downloader=StaticImageDownloader(image),
    ).send(make_anhoch_message(), lambda _: True)

    # Then
    assert delivered
    assert arguments["files"] == {
        "files[0]": ("product-image.jpg", b"image-bytes", "image/jpeg"),
    }
    data = arguments["data"]
    assert isinstance(data, dict)
    payload_json = data["payload_json"]
    assert isinstance(payload_json, str)
    payload = json.loads(payload_json)
    assert payload["attachments"] == [{"id": 0, "filename": "product-image.jpg"}]
    assert (
        payload["components"][0]["components"][0]["accessory"]["media"]["url"]
        == "attachment://product-image.jpg"
    )


def test_delivery_omits_unavailable_anhoch_thumbnail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    arguments: dict[str, PostArgument] = {}

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        del url
        arguments.update(kwargs)
        return make_response(200)

    monkeypatch.setattr(session, "post", post)

    # When
    delivered = DiscordWebhookClient(
        session,
        image_downloader=StaticImageDownloader(None),
    ).send(make_anhoch_message(), lambda _: True)

    # Then
    assert delivered
    assert "files" not in arguments
    payload = arguments["json"]
    assert isinstance(payload, dict)
    components = payload["components"]
    assert isinstance(components, list)
    first_component = components[0]
    assert isinstance(first_component, dict)
    children = first_component["components"]
    assert isinstance(children, list)
    first_child = children[0]
    assert isinstance(first_child, dict)
    assert "accessory" not in first_child


def test_delivery_retries_without_thumbnail_when_discord_rejects_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    responses = [make_response(415), make_response(200)]
    attempts: list[dict[str, PostArgument]] = []

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        del url
        attempts.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(session, "post", post)
    image = DownloadedImage(
        filename="product-image.jpg",
        content_type="image/jpeg",
        content=b"image-bytes",
    )

    # When
    delivered = DiscordWebhookClient(
        session,
        image_downloader=StaticImageDownloader(image),
    ).send(make_anhoch_message(), lambda _: True)

    # Then
    assert delivered
    assert len(attempts) == 2
    assert "files" in attempts[0]
    assert "files" not in attempts[1]
    fallback_payload = attempts[1]["json"]
    assert isinstance(fallback_payload, dict)
    components = fallback_payload["components"]
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    children = container["components"]
    assert isinstance(children, list)
    first_child = children[0]
    assert isinstance(first_child, dict)
    assert "accessory" not in first_child


def test_thumbnail_fallback_remains_selected_after_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    attempts: list[dict[str, PostArgument]] = []

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        del url
        attempts.append(kwargs)
        if len(attempts) == 1:
            return make_response(415)
        if len(attempts) == 2:
            raise requests.ConnectionError("connection reset")
        return make_response(200)

    monkeypatch.setattr(session, "post", post)
    image = DownloadedImage(
        filename="product-image.jpg",
        content_type="image/jpeg",
        content=b"image-bytes",
    )
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    # When
    delivered = DiscordWebhookClient(
        session,
        image_downloader=StaticImageDownloader(image),
    ).send(make_anhoch_message(), record_delay)

    # Then
    assert delivered
    assert ["files" in attempt for attempt in attempts] == [True, False, False]
    assert delays == [2.0]


def test_delivery_requests_components_and_server_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    arguments: dict[str, PostArgument] = {}

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        del url
        arguments.update(kwargs)
        return make_response(200)

    monkeypatch.setattr(session, "post", post)

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), lambda _: True)

    # Then
    assert delivered
    assert arguments["params"] == {
        "wait": "true",
        "with_components": "true",
    }


def test_delivery_rejects_response_without_created_message_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        del url, kwargs
        return make_response(204)

    monkeypatch.setattr(session, "post", post)

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), lambda _: True)

    # Then
    assert not delivered


def test_request_failure_does_not_log_webhook_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    webhook_url = "https://discord.test/api/webhooks/id/secret-token"
    session = requests.Session()

    def fail_request(url: str, **kwargs: PostArgument) -> requests.Response:
        del kwargs
        raise requests.ConnectionError(f"Could not connect to {url}")

    monkeypatch.setattr(session, "post", fail_request)
    client = DiscordWebhookClient(session)
    message = make_message(webhook_url)
    caplog.set_level(logging.ERROR)

    # When
    delivered = client.send(message, lambda seconds: True)

    # Then
    assert not delivered
    assert webhook_url not in caplog.text
    assert "ConnectionError" in caplog.text


def test_connection_error_is_retried_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        if attempts == 1:
            raise requests.ConnectionError("connection reset")
        return make_response(200)

    monkeypatch.setattr(session, "post", post)
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), record_delay)

    # Then
    assert delivered
    assert attempts == 2
    assert delays == [2.0]


def test_timeout_error_is_retried_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        if attempts == 1:
            raise requests.Timeout("read timed out")
        return make_response(200)

    monkeypatch.setattr(session, "post", post)
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), record_delay)

    # Then
    assert delivered
    assert attempts == 2
    assert delays == [2.0]


def test_server_error_is_retried_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    responses = [make_response(503), make_response(200)]
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        return responses.pop(0)

    monkeypatch.setattr(session, "post", post)
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), record_delay)

    # Then
    assert delivered
    assert attempts == 2
    assert delays == [2.0]


def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    session = requests.Session()
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        return make_response(400)

    monkeypatch.setattr(session, "post", post)

    def unexpected_delay(seconds: float) -> bool:
        raise AssertionError(f"unexpected retry delay: {seconds}")

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), unexpected_delay)

    # Then
    assert not delivered
    assert attempts == 1


@pytest.mark.parametrize(
    ("retry_after", "expected_delays"),
    [
        ("0.25", [0.25, 0.25, 0.25]),
        ("86400", [300.0, 300.0, 300.0]),
        ("inf", [2.0, 4.0, 8.0]),
        ("-1", [2.0, 4.0, 8.0]),
    ],
)
def test_final_rate_limit_response_honors_bounded_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
    expected_delays: list[float],
) -> None:
    # Given
    session = requests.Session()
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        return make_response(429, retry_after=retry_after)

    monkeypatch.setattr(session, "post", post)
    delays: list[float] = []

    def record_delay(seconds: float) -> bool:
        delays.append(seconds)
        return True

    # When
    delivered = DiscordWebhookClient(session).send(make_message(), record_delay)

    # Then
    assert not delivered
    assert attempts == 3
    assert delays == expected_delays
