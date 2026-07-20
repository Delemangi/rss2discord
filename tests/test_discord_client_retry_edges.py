import logging
from typing import Literal, assert_never

import pytest
import requests

from configuration import FeedConfig
from discord_client import (
    DiscordWebhookClient,
    JSONValue,
    SleepCallback,
    WebhookMessage,
)
from models import EntryData

type PostArgument = int | dict[str, str] | dict[str, JSONValue]


def make_message() -> WebhookMessage:
    return WebhookMessage(
        feed=FeedConfig(
            id="news",
            name="News",
            url="https://example.test/feed.xml",
            webhook="https://discord.test/api/webhooks/id/token",
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


def record_delays(delays: list[float], *, continue_waiting: bool) -> SleepCallback:
    def sleep(seconds: float) -> bool:
        delays.append(seconds)
        return continue_waiting

    return sleep


def test_invalid_retry_after_falls_back_to_exponential_delay(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    session = requests.Session()
    responses = [
        make_response(429, retry_after="invalid"),
        make_response(429, retry_after="invalid"),
        make_response(200),
    ]
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        return responses.pop(0)

    monkeypatch.setattr(session, "post", post)
    delays: list[float] = []
    caplog.set_level(logging.WARNING)

    # When
    delivered = DiscordWebhookClient(session).send(
        make_message(),
        record_delays(delays, continue_waiting=True),
    )

    # Then
    assert delivered
    assert attempts == 3
    assert delays == [2.0, 4.0]
    assert "invalid Retry-After header" in caplog.text


def test_final_server_error_response_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session = requests.Session()
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        return make_response(503)

    monkeypatch.setattr(session, "post", post)
    delays: list[float] = []

    # When
    delivered = DiscordWebhookClient(session).send(
        make_message(),
        record_delays(delays, continue_waiting=True),
    )

    # Then
    assert not delivered
    assert attempts == 3
    assert delays == [2.0, 4.0]


@pytest.mark.parametrize(
    ("scenario", "expected_delay"),
    [
        ("connection", 2.0),
        ("rate_limit", 0.25),
        ("server", 2.0),
    ],
)
def test_shutdown_during_retry_sleep_aborts(
    monkeypatch: pytest.MonkeyPatch,
    scenario: Literal["connection", "rate_limit", "server"],
    expected_delay: float,
) -> None:
    # Given
    session = requests.Session()
    attempts = 0

    def post(url: str, **kwargs: PostArgument) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        match scenario:
            case "connection":
                raise requests.ConnectionError("connection reset")
            case "rate_limit":
                return make_response(429, retry_after="0.25")
            case "server":
                return make_response(503)
            case unreachable:
                assert_never(unreachable)

    monkeypatch.setattr(session, "post", post)
    delays: list[float] = []

    # When
    delivered = DiscordWebhookClient(session).send(
        make_message(),
        record_delays(delays, continue_waiting=False),
    )

    # Then
    assert not delivered
    assert attempts == 1
    assert delays == [expected_delay]
