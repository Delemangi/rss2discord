import logging

import pytest
import requests

from configuration import FeedConfig
from discord_client import DiscordWebhookClient, WebhookMessage
from models import EntryData


def test_request_failure_does_not_log_webhook_url(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    webhook_url = "https://discord.test/api/webhooks/id/secret-token"
    session = requests.Session()

    def fail_request(url: str, **kwargs: object) -> requests.Response:
        del kwargs
        raise requests.ConnectionError(f"Could not connect to {url}")

    monkeypatch.setattr(session, "post", fail_request)
    client = DiscordWebhookClient(session)
    message = WebhookMessage(
        feed=FeedConfig(
            id="news",
            name="News",
            url="https://example.test/feed.xml",
            webhook=webhook_url,
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
    caplog.set_level(logging.ERROR)

    # When
    delivered = client.send(message, lambda seconds: True)

    # Then
    assert not delivered
    assert webhook_url not in caplog.text
    assert "ConnectionError" in caplog.text
