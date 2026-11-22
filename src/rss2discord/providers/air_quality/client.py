"""Bounded Open-Meteo air-quality API client."""

import json
from collections.abc import Callable
from urllib.parse import urlsplit

import requests
from pydantic import ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.air_quality.bounds import (
    AIR_QUALITY_HOST,
    AIR_QUALITY_LABEL,
    AIR_QUALITY_PATH,
    AIR_QUALITY_REQUEST_SECONDS,
    AIR_QUALITY_STREAM_CHUNK_BYTES,
    AIR_QUALITY_USER_AGENT,
    MAX_AIR_QUALITY_RESPONSE_BYTES,
)
from rss2discord.providers.air_quality.models import (
    AirQualityReading,
    AirQualityResponse,
)
from rss2discord.retries import parse_retry_after
from rss2discord.transports.bounded_http import read_bounded_content

FetchBytes = Callable[[str], bytes]


class AirQualityClient:
    """Fetch the current air-quality observation for one location."""

    def __init__(self, fetch: FetchBytes | None = None) -> None:
        self._fetch = fetch or _fetch_content

    def fetch_reading(self, url: str) -> AirQualityReading:
        validate_air_quality_url(url)
        return _parse_reading(self._fetch(url))


def validate_air_quality_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(AIR_QUALITY_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != AIR_QUALITY_HOST
        or port not in {None, 443}
        or parsed.path != AIR_QUALITY_PATH
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FeedFetchError(AIR_QUALITY_LABEL, "InvalidUrl")
    return url


def _fetch_content(url: str) -> bytes:
    try:
        with requests.get(
            url,
            headers={"User-Agent": AIR_QUALITY_USER_AGENT},
            timeout=AIR_QUALITY_REQUEST_SECONDS,
            stream=True,
        ) as response:
            try:
                response.raise_for_status()
            except requests.HTTPError:
                status_code = response.status_code
                raise FeedFetchError(
                    AIR_QUALITY_LABEL,
                    "HTTPError",
                    status_code=status_code,
                    retryable=status_code == 429 or 500 <= status_code < 600,
                    retry_after=parse_retry_after(
                        response.headers.get("Retry-After"),
                    ),
                ) from None
            return read_bounded_content(
                response,
                label=AIR_QUALITY_LABEL,
                max_bytes=MAX_AIR_QUALITY_RESPONSE_BYTES,
                chunk_bytes=AIR_QUALITY_STREAM_CHUNK_BYTES,
            )
    except FeedFetchError:
        raise
    except (requests.ConnectionError, requests.Timeout) as error:
        raise FeedFetchError(
            AIR_QUALITY_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None
    except requests.RequestException as error:
        raise FeedFetchError(
            AIR_QUALITY_LABEL,
            type(error).__name__,
        ) from None


def _parse_reading(content: bytes) -> AirQualityReading:
    try:
        payload = json.loads(content)
        response = AirQualityResponse.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        raise FeedFetchError(AIR_QUALITY_LABEL, "InvalidResponse") from None
    current = response.current
    if not current.time.strip():
        raise FeedFetchError(AIR_QUALITY_LABEL, "InvalidResponse")
    values = (
        current.european_aqi,
        current.pm10,
        current.pm2_5,
        current.us_aqi,
    )
    if any(value < 0 for value in values):
        raise FeedFetchError(AIR_QUALITY_LABEL, "InvalidResponse")
    return AirQualityReading(
        observed_at=current.time,
        timezone=response.timezone,
        european_aqi=current.european_aqi,
        pm10=current.pm10,
        pm2_5=current.pm2_5,
        us_aqi=current.us_aqi,
    )
