import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

import requests

from configuration import FeedConfig
from discord_components import JSONValue, build_components_v2_payload
from models import EntryData

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_RETRY_DELAY_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 300.0

SleepCallback = Callable[[float], bool]


class _DeliveryAction(Enum):
    DELIVERED = auto()
    RETRY = auto()
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class _DeliveryResult:
    action: _DeliveryAction
    wait_time: float = 0.0


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
            result = self._attempt_delivery(message, payload, attempt)
            if result.action is _DeliveryAction.FAILED:
                if result.wait_time > 0:
                    sleep(result.wait_time)
                return False
            if result.action is _DeliveryAction.DELIVERED:
                logger.info(
                    "Sent entry %s to Discord for feed %s",
                    message.entry.title,
                    message.feed.id,
                )
                return True
            if not sleep(result.wait_time):
                return False

        return False

    def _attempt_delivery(
        self,
        message: WebhookMessage,
        payload: dict[str, JSONValue],
        attempt: int,
    ) -> _DeliveryResult:
        try:
            response = self._session.post(
                message.feed.webhook,
                json=payload,
                headers={"Content-Type": "application/json"},
                params={"with_components": "true"},
                timeout=10,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            return self._handle_retryable_request_error(message, error, attempt)
        except requests.RequestException as error:
            self._log_request_error("request failed", message.feed.id, error)
            return _DeliveryResult(_DeliveryAction.FAILED)

        return self._classify_response(message, response, attempt)

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
