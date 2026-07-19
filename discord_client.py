import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import requests

from configuration import FeedConfig
from models import EntryData

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
            except requests.Timeout:
                logger.warning(
                    "Discord request timed out for feed %s on attempt %d/%d",
                    message.feed.id,
                    attempt + 1,
                    MAX_RETRIES,
                )
                if not self._wait_before_retry(attempt, sleep):
                    return False
                continue
            except requests.RequestException as error:
                self._log_request_error("request failed", message.feed.id, error)
                return False

            if response.status_code == 429:
                wait_time = self._retry_after(response, attempt)
                logger.warning(
                    "Discord rate limited feed %s; retrying in %.1f seconds",
                    message.feed.id,
                    wait_time,
                )
                if not sleep(wait_time):
                    return False
                continue

            try:
                response.raise_for_status()
            except requests.RequestException as error:
                self._log_request_error("rejected message", message.feed.id, error)
                return False

            logger.info(
                "Sent entry %s to Discord for feed %s",
                message.entry.title,
                message.feed.id,
            )
            return True

        logger.error("Discord delivery retries exhausted for feed %s", message.feed.id)
        return False

    @staticmethod
    def _build_payload(message: WebhookMessage) -> dict[str, object]:
        embed: dict[str, object] = {
            "title": message.entry.title,
            "url": message.entry.link,
            "description": message.entry.description,
            "color": message.feed.embed_color or DEFAULT_EMBED_COLOR,
            "footer": {"text": message.source_title},
        }
        if message.entry.author:
            embed["author"] = {"name": message.entry.author}
        if message.entry.timestamp is not None:
            embed["timestamp"] = message.entry.timestamp

        payload: dict[str, object] = {"embeds": [embed]}
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
        return BASE_RETRY_DELAY_SECONDS * (2**attempt)

    @staticmethod
    def _wait_before_retry(attempt: int, sleep: SleepCallback) -> bool:
        if attempt >= MAX_RETRIES - 1:
            return True
        return sleep(BASE_RETRY_DELAY_SECONDS * (2**attempt))

    @staticmethod
    def _log_request_error(action: str, feed_id: str, error: Exception) -> None:
        logger.error(
            "Discord %s for feed %s (%s)",
            action,
            feed_id,
            type(error).__name__,
        )
