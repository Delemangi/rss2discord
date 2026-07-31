import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Final, Protocol

import requests

from rss2discord.discord.components import JSONValue, build_components_v2_payload
from rss2discord.discord.image_retries import ImageDownloadInterruptedError
from rss2discord.discord.images import AnhochImageDownloader, ImageDownloader
from rss2discord.discord.message import (
    PreparedDelivery,
    WebhookMessage,
    prepare_delivery,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 300.0
MEDIA_REJECTION_STATUS_CODES: Final = frozenset({400, 413, 415})

SleepCallback = Callable[[float], bool]


class _DeliveryAction(Enum):
    DELIVERED = auto()
    RETRY = auto()
    FAILED = auto()


class DiscordDeliveryResult(Enum):
    """The observable result of one Discord webhook delivery attempt."""

    DELIVERED = auto()
    FAILED = auto()
    INTERRUPTED = auto()

    def __bool__(self) -> bool:
        return self is DiscordDeliveryResult.DELIVERED


@dataclass(frozen=True, slots=True)
class _DeliveryResult:
    action: _DeliveryAction
    wait_time: float = 0.0
    drop_image: bool = False


class DiscordSender(Protocol):
    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult: ...


class DiscordWebhookClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        image_downloader: ImageDownloader | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._image_downloader = image_downloader

    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        try:
            delivery = self._prepare_delivery(message, sleep)
        except ImageDownloadInterruptedError:
            return DiscordDeliveryResult.INTERRUPTED

        for attempt in range(MAX_RETRIES):
            result = self._attempt_delivery(delivery, attempt)
            if result.drop_image and delivery.fallback_request is not None:
                delivery = replace(
                    delivery,
                    request=delivery.fallback_request,
                    fallback_request=None,
                )
            if result.action is _DeliveryAction.FAILED:
                if result.wait_time > 0 and not sleep(result.wait_time):
                    return DiscordDeliveryResult.INTERRUPTED
                return DiscordDeliveryResult.FAILED
            if result.action is _DeliveryAction.DELIVERED:
                logger.info(
                    "Sent entry %.256r to Discord for feed %s",
                    message.entry.title,
                    message.feed.id,
                )
                return DiscordDeliveryResult.DELIVERED
            if not sleep(result.wait_time):
                return DiscordDeliveryResult.INTERRUPTED

        return DiscordDeliveryResult.FAILED

    def _attempt_delivery(
        self,
        delivery: PreparedDelivery,
        attempt: int,
    ) -> _DeliveryResult:
        message = delivery.message
        drop_image = False
        try:
            response = delivery.request.post(self._session)
            if (
                delivery.request.image is not None
                and response.status_code in MEDIA_REJECTION_STATUS_CODES
                and delivery.fallback_request is not None
            ):
                logger.warning(
                    "Discord rejected thumbnail for feed %s (HTTP %d); "
                    "retrying without it",
                    message.feed.id,
                    response.status_code,
                )
                drop_image = True
                response = delivery.fallback_request.post(self._session)
        except (requests.ConnectionError, requests.Timeout) as error:
            result = self._handle_retryable_request_error(message, error, attempt)
            return _DeliveryResult(result.action, result.wait_time, drop_image)
        except requests.RequestException as error:
            self._log_request_error("request failed", message.feed.id, error)
            return _DeliveryResult(_DeliveryAction.FAILED, drop_image=drop_image)

        result = self._classify_response(message, response, attempt)
        return _DeliveryResult(result.action, result.wait_time, drop_image)

    def _prepare_delivery(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> PreparedDelivery:
        image_downloader = self._image_downloader
        if image_downloader is None:
            image_downloader = AnhochImageDownloader(sleep=sleep)
        return prepare_delivery(message, image_downloader, sleep)

    def _handle_retryable_request_error(
        self,
        message: WebhookMessage,
        error: requests.RequestException,
        attempt: int,
    ) -> _DeliveryResult:
        if self._is_final_attempt(attempt):
            self._log_request_error(
                "request retries exhausted",
                message.feed.id,
                error,
            )
            return _DeliveryResult(_DeliveryAction.FAILED)

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
        return _DeliveryResult(_DeliveryAction.RETRY, wait_time)

    def _classify_response(
        self,
        message: WebhookMessage,
        response: requests.Response,
        attempt: int,
    ) -> _DeliveryResult:
        if response.status_code == 429:
            return self._handle_rate_limit(message, response, attempt)
        if 500 <= response.status_code < 600:
            return self._handle_server_error(message, response, attempt)

        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            self._log_request_error("rejected message", message.feed.id, error)
            return _DeliveryResult(_DeliveryAction.FAILED)
        if not response.content:
            logger.error(
                "Discord returned no delivery confirmation for feed %s",
                message.feed.id,
            )
            return _DeliveryResult(_DeliveryAction.FAILED)
        return _DeliveryResult(_DeliveryAction.DELIVERED)

    def _handle_rate_limit(
        self,
        message: WebhookMessage,
        response: requests.Response,
        attempt: int,
    ) -> _DeliveryResult:
        wait_time = self._retry_after(response, attempt)
        if self._is_final_attempt(attempt):
            logger.error(
                "Discord rate limit retries exhausted for feed %s",
                message.feed.id,
            )
            return _DeliveryResult(_DeliveryAction.FAILED, wait_time)

        logger.warning(
            "Discord rate limited feed %s; retrying in %.1f seconds",
            message.feed.id,
            wait_time,
        )
        return _DeliveryResult(_DeliveryAction.RETRY, wait_time)

    def _handle_server_error(
        self,
        message: WebhookMessage,
        response: requests.Response,
        attempt: int,
    ) -> _DeliveryResult:
        if self._is_final_attempt(attempt):
            logger.error(
                "Discord server retries exhausted for feed %s (HTTP %d)",
                message.feed.id,
                response.status_code,
            )
            return _DeliveryResult(_DeliveryAction.FAILED)

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
        return _DeliveryResult(_DeliveryAction.RETRY, wait_time)

    @staticmethod
    def _is_final_attempt(attempt: int) -> bool:
        return attempt >= MAX_RETRIES - 1

    @staticmethod
    def _build_payload(message: WebhookMessage) -> dict[str, JSONValue]:
        return build_components_v2_payload(
            message.feed,
            message.entry,
            message.source_title,
        )

    @staticmethod
    def _retry_after(response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                wait_time = float(retry_after)
            except ValueError:
                logger.warning("Discord returned an invalid Retry-After header")
            else:
                if math.isfinite(wait_time) and wait_time >= 0:
                    return min(wait_time, MAX_RETRY_AFTER_SECONDS)
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
