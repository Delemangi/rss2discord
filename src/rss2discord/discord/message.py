import logging
from dataclasses import dataclass, replace

from rss2discord.configuration import FeedConfig
from rss2discord.discord.components import build_components_v2_payload
from rss2discord.discord.image_retries import ImageDownloadInterruptedError, RetrySleep
from rss2discord.discord.images import DownloadedImage, ImageDownloader
from rss2discord.discord.request import DiscordRequest
from rss2discord.models import EntryData

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookMessage:
    feed: FeedConfig
    entry: EntryData
    source_title: str


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    message: WebhookMessage
    request: DiscordRequest
    fallback_request: DiscordRequest | None


def prepare_delivery(
    message: WebhookMessage,
    image_downloader: ImageDownloader,
    sleep: RetrySleep,
) -> PreparedDelivery:
    image: DownloadedImage | None = None
    rendered_entry = message.entry
    if message.feed.strategy == "anhoch" and message.entry.image_url is not None:
        image = image_downloader.download(message.entry.image_url)
        if not sleep(0):
            raise ImageDownloadInterruptedError
        rendered_entry = replace(message.entry, image_url=None)
        if image is None:
            logger.warning("Anhoch thumbnail unavailable for feed %s", message.feed.id)
    payload = build_components_v2_payload(
        message.feed,
        rendered_entry,
        message.source_title,
        attachment_filename=image.filename if image is not None else None,
    )
    fallback_request: DiscordRequest | None = None
    if image is not None:
        payload["attachments"] = [{"id": 0, "filename": image.filename}]
        fallback_request = DiscordRequest(
            webhook=message.feed.webhook,
            payload=build_components_v2_payload(
                message.feed,
                rendered_entry,
                message.source_title,
            ),
            image=None,
        )
    return PreparedDelivery(
        message=message,
        request=DiscordRequest(
            webhook=message.feed.webhook,
            payload=payload,
            image=image,
        ),
        fallback_request=fallback_request,
    )
