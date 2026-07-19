import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import requests

from configuration import FeedConfig
from models import EntryData

type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)

logger = logging.getLogger(__name__)

DEFAULT_EMBED_COLOR = 5814783
MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 2.0

SleepCallback = Callable[[float], bool]


@dataclass(frozen=True, slots=True)
class WebhookMessage:
    feed: FeedConfig
    entry: EntryData
    source_title: str


class DiscordSender(Protocol):
    def send(self, message: WebhookMessage, sleep: SleepCallback) -> bool: ...


class DiscordWebhookClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def send(self, message: WebhookMessage, sleep: SleepCallback) -> bool:
        payload = self._build_payload(message)

        for attempt in range(MAX_RETRIES):
            try:
                response = self._session.post(
                    message.feed.webhook,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt >= MAX_RETRIES - 1:
                    self._log_request_error(
                        "request retries exhausted",
                        message.feed.id,
                        error,
                    )
                    return False
                wait_time = self._retry_delay(attempt)
                logger.warning(
                    "Discord request failed for feed %s on attempt %d/%d; "
                    "retrying in %.1f seconds (%s)",
                    message.feed.id,
                    attempt + 1,
                    MAX_RETRIES,
                    wait_time,
                    type(error).__name__,
                )
                if not sleep(wait_time):
                    return False
                continue
            except requests.RequestException as error:
                self._log_request_error("request failed", message.feed.id, error)
                return False

            if response.status_code == 429:
                if attempt >= MAX_RETRIES - 1:
                    logger.error(
                        "Discord rate limit retries exhausted for feed %s",
                        message.feed.id,
                    )
                    return False
                wait_time = self._retry_after(response, attempt)
                logger.warning(
                    "Discord rate limited feed %s; retrying in %.1f seconds",
                    message.feed.id,
                    wait_time,
                )
                if not sleep(wait_time):
                    return False
                continue

            if 500 <= response.status_code < 600:
                if attempt >= MAX_RETRIES - 1:
                    logger.error(
                        "Discord server retries exhausted for feed %s (HTTP %d)",
                        message.feed.id,
                        response.status_code,
                    )
                    return False
                wait_time = self._retry_delay(attempt)
                logger.warning(
                    "Discord server error for feed %s on attempt %d/%d; "
                    "retrying in %.1f seconds (HTTP %d)",
                    message.feed.id,
                    attempt + 1,
                    MAX_RETRIES,
                    wait_time,
                    response.status_code,
                )
                if not sleep(wait_time):
                    return False
                continue

            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                self._log_request_error("rejected message", message.feed.id, error)
                return False

            logger.info(
                "Sent entry %s to Discord for feed %s",
                message.entry.title,
                message.feed.id,
            )
            return True

        return False

    @staticmethod
    def _build_payload(message: WebhookMessage) -> dict[str, JSONValue]:
        embed: dict[str, JSONValue] = {
            "title": message.entry.title,
            "url": message.entry.link,
            "description": message.entry.description,
            "color": (
                message.feed.embed_color
                if message.feed.embed_color is not None
                else DEFAULT_EMBED_COLOR
            ),
            "footer": {"text": message.source_title},
        }
        if message.entry.author:
            embed["author"] = {"name": message.entry.author}
        if message.entry.timestamp is not None:
            embed["timestamp"] = message.entry.timestamp

        payload: dict[str, JSONValue] = {"embeds": [embed]}
        if message.feed.webhook_name:
            payload["username"] = message.feed.webhook_name
        if message.feed.webhook_avatar:
            payload["avatar_url"] = message.feed.webhook_avatar
        return payload

    @staticmethod
    def _retry_after(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                logger.warning("Discord returned an invalid Retry-After header")
        return DiscordWebhookClient._retry_delay(attempt)

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return BASE_RETRY_DELAY_SECONDS * (2**attempt)

    @staticmethod
    def _log_request_error(action: str, feed_id: str, error: Exception) -> None:
        logger.error(
            "Discord %s for feed %s (%s)",
            action,
            feed_id,
            type(error).__name__,
        )
