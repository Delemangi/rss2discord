from dataclasses import dataclass, replace

from rss2discord.configuration import FeedConfig
from rss2discord.discord.components import build_components_v2_payload
from rss2discord.discord.images import DownloadedImage, ImageDownloader
from rss2discord.discord.request import DiscordRequest
from rss2discord.models import EntryData


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
) -> PreparedDelivery:
    image: DownloadedImage | None = None
    rendered_message = message
    if message.feed.strategy == "anhoch" and message.entry.image_url is not None:
        image = image_downloader.download(message.entry.image_url)
        rendered_message = replace(
            message,
            entry=replace(
                message.entry,
                image_url=(
                    f"attachment://{image.filename}" if image is not None else None
                ),
            ),
        )
    payload = build_components_v2_payload(
        rendered_message.feed,
        rendered_message.entry,
        rendered_message.source_title,
    )
    fallback_request: DiscordRequest | None = None
    if image is not None:
        payload["attachments"] = [{"id": 0, "filename": image.filename}]
        fallback_message = replace(
            message,
            entry=replace(message.entry, image_url=None),
        )
        fallback_request = DiscordRequest(
            webhook=message.feed.webhook,
            payload=build_components_v2_payload(
                fallback_message.feed,
                fallback_message.entry,
                fallback_message.source_title,
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
