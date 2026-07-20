import logging

import pytest
import requests

from configuration import FeedConfig
from discord_client import DiscordWebhookClient, JSONValue, WebhookMessage
from models import EntryData

type PostArgument = int | dict[str, str] | dict[str, JSONValue]


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
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


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
        return make_response(204)

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
        return make_response(204)

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
    responses = [make_response(503), make_response(204)]
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


def test_final_rate_limit_response_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        return make_response(429, retry_after="0.25")

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
    assert delays == [0.25, 0.25]


def test_zero_embed_color_is_preserved() -> None:
    # Given
    message = make_message(embed_color=0)

    # When
    payload = DiscordWebhookClient._build_payload(message)

    # Then
    embeds = payload["embeds"]
    assert isinstance(embeds, list)
    embed = embeds[0]
    assert isinstance(embed, dict)
    assert embed["color"] == 0
