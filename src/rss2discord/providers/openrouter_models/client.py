"""Bounded OpenRouter model catalog client."""

import json
from collections.abc import Callable
from urllib.parse import urlsplit

import requests
from pydantic import ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.openrouter_models.bounds import (
    MAX_OPENROUTER_RESPONSE_BYTES,
    OPENROUTER_HOST,
    OPENROUTER_LABEL,
    OPENROUTER_MODELS_PATH,
    OPENROUTER_REQUEST_SECONDS,
    OPENROUTER_STREAM_CHUNK_BYTES,
    OPENROUTER_USER_AGENT,
)
from rss2discord.providers.openrouter_models.models import (
    OpenRouterModel,
    OpenRouterModelsResponse,
)
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    parse_retry_after,
)
from rss2discord.transports.bounded_http import read_bounded_content

FetchBytes = Callable[[str], bytes]


class OpenRouterModelsClient:
    """Fetch and validate the public OpenRouter model catalog."""

    def __init__(self, fetch: FetchBytes | None = None) -> None:
        self._fetch = fetch or _fetch_content

    def fetch_models(self, url: str) -> tuple[OpenRouterModel, ...]:
        validate_openrouter_models_url(url)
        return _parse_models(self._fetch(url))

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[OpenRouterModel, ...]:
        """Fetch the catalog through the shared price-monitor interface."""
        if is_shutdown_requested():
            raise FeedFetchInterruptedError

        def operation() -> tuple[OpenRouterModel, ...]:
            return self.fetch_models(url)

        return (
            retry_policy.execute(operation) if retry_policy is not None else operation()
        )


def validate_openrouter_models_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(OPENROUTER_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != OPENROUTER_HOST
        or port not in {None, 443}
        or parsed.path != OPENROUTER_MODELS_PATH
        or parsed.query
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FeedFetchError(OPENROUTER_LABEL, "InvalidUrl")
    return url


def _fetch_content(url: str) -> bytes:
    try:
        with requests.get(
            url,
            headers={"User-Agent": OPENROUTER_USER_AGENT},
            timeout=OPENROUTER_REQUEST_SECONDS,
            stream=True,
        ) as response:
            try:
                response.raise_for_status()
            except requests.HTTPError:
                status_code = response.status_code
                raise FeedFetchError(
                    OPENROUTER_LABEL,
                    "HTTPError",
                    status_code=status_code,
                    retryable=status_code == 429 or 500 <= status_code < 600,
                    retry_after=parse_retry_after(
                        response.headers.get("Retry-After"),
                    ),
                ) from None
            return read_bounded_content(
                response,
                label=OPENROUTER_LABEL,
                max_bytes=MAX_OPENROUTER_RESPONSE_BYTES,
                chunk_bytes=OPENROUTER_STREAM_CHUNK_BYTES,
            )
    except FeedFetchError:
        raise
    except (requests.ConnectionError, requests.Timeout) as error:
        raise FeedFetchError(
            OPENROUTER_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None
    except requests.RequestException as error:
        raise FeedFetchError(
            OPENROUTER_LABEL,
            type(error).__name__,
        ) from None


def _parse_models(content: bytes) -> tuple[OpenRouterModel, ...]:
    try:
        payload = json.loads(content)
        response = OpenRouterModelsResponse.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        raise FeedFetchError(OPENROUTER_LABEL, "InvalidResponse") from None
    if not response.data:
        raise FeedFetchError(OPENROUTER_LABEL, "InvalidResponse")
    return response.data
