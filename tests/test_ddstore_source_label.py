from rss2discord.configuration import FeedConfig
from rss2discord.discord.message import WebhookMessage
from rss2discord.models import EntryData
from tests.discord_components_helpers import get_metadata_content


def test_components_v2_payload_renders_ddstore_source_label() -> None:
    # Given
    message = WebhookMessage(
        feed=FeedConfig(
            id="ddstore",
            name="News",
            url="https://ddstore.mk/",
            webhook="https://discord.test/api/webhooks/id/token",
            strategy="ddstore",
        ),
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="",
            author="",
            timestamp=None,
        ),
        source_title="News",
    )

    # When
    metadata = get_metadata_content(message)

    # Then
    assert metadata.startswith("-# DDStore • ")
