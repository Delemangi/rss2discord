import logging

import pytest
import requests

from rss2discord.configuration import FeedConfig
from rss2discord.discord.client import DiscordWebhookClient, JSONValue, WebhookMessage
from rss2discord.models import EntryData

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
    if status_code == 200:
        response._content = b'{"id":"123"}'
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


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
