import logging
from dataclasses import dataclass, replace

from rss2discord.configuration import FeedConfig
from rss2discord.discord.components import (
    IS_COMPONENTS_V2,
    JSONValue,
    build_components_v2_payload,
)
from rss2discord.discord.image_retries import ImageDownloadInterruptedError, RetrySleep
from rss2discord.discord.image_urls import IMAGE_SOURCE_BY_STRATEGY
from rss2discord.discord.images import DownloadedImage, ImageDownloader
from rss2discord.discord.request import DiscordRequest
from rss2discord.models import EntryData

logger = logging.getLogger(__name__)

FILE_COMPONENT: int = 13
ATTACHMENT_URL_PREFIX = "attachment://"


@dataclass(frozen=True, slots=True)
class ReportFile:
    """One uploaded report attachment referenced from the message payload."""

    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class WebhookMessage:
    feed: FeedConfig
    entry: EntryData
    source_title: str
    files: tuple[ReportFile, ...] = ()
    # Prebuilt components for multi-card messages such as the bulk digest.
    components: tuple[JSONValue, ...] | None = None


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
    image_source = IMAGE_SOURCE_BY_STRATEGY.get(message.feed.strategy)
    if image_source is not None and message.entry.image_url is not None:
        image = image_downloader.download(message.entry.image_url)
        if not sleep(0):
            raise ImageDownloadInterruptedError
        rendered_entry = replace(message.entry, image_url=None)
        if image is None:
            logger.warning(
                "%s thumbnail unavailable for feed %s",
                message.feed.name or message.source_title,
                message.feed.id,
            )
    payload = _render_payload(message, rendered_entry, image)
    fallback_request: DiscordRequest | None = None
    if image is not None:
        fallback_payload = _render_payload(
            replace(message, components=None),
            rendered_entry,
            None,
        )
        fallback_request = DiscordRequest(
            webhook=message.feed.webhook,
            payload=fallback_payload,
            image=None,
            files=message.files,
        )
    return PreparedDelivery(
        message=message,
        request=DiscordRequest(
            webhook=message.feed.webhook,
            payload=payload,
            image=image,
            files=message.files,
        ),
        fallback_request=fallback_request,
    )


def _render_payload(
    message: WebhookMessage,
    rendered_entry: EntryData,
    image: DownloadedImage | None,
) -> dict[str, JSONValue]:
    if message.components is None:
        payload = build_components_v2_payload(
            message.feed,
            rendered_entry,
            message.source_title,
            attachment_filename=image.filename if image is not None else None,
        )
        raw_components = payload.get("components")
        components: list[JSONValue] = (
            list(raw_components) if isinstance(raw_components, list) else []
        )
    else:
        payload = {
            "allowed_mentions": {"parse": []},
            "components": [],
            "flags": IS_COMPONENTS_V2,
        }
        components = list(message.components)
        if message.feed.webhook_name:
            payload["username"] = message.feed.webhook_name
        if message.feed.webhook_avatar:
            payload["avatar_url"] = message.feed.webhook_avatar
    if message.files:
        components.extend(_file_component(report.filename) for report in message.files)
    payload["components"] = components
    attachments: list[JSONValue] = []
    if image is not None:
        attachments.append({"id": 0, "filename": image.filename})
    attachments.extend(
        {"id": index, "filename": report.filename}
        for index, report in enumerate(
            message.files,
            start=len(attachments),
        )
    )
    if attachments:
        payload["attachments"] = attachments
    return payload


def _file_component(filename: str) -> dict[str, JSONValue]:
    return {
        "file": {"url": f"{ATTACHMENT_URL_PREFIX}{filename}"},
        "type": FILE_COMPONENT,
    }
