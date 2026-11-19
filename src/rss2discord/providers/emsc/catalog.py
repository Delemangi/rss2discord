"""Bounded EMSC FDSN event traversal."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.emsc.bounds import (
    EMSC_EVENT_TYPES,
    EMSC_LABEL,
    EMSC_REQUEST_SECONDS,
    EMSC_STREAM_CHUNK_BYTES,
    EMSC_USER_AGENT,
    MAX_EMSC_EVENTS,
    MAX_EMSC_RESPONSE_BYTES,
)
from rss2discord.providers.emsc.models import EarthquakeEvent
from rss2discord.retries import parse_retry_after


class EmscCatalogClient:
    """Fetch one bounded EMSC FDSN event query."""

    def fetch_events(self, url: str) -> tuple[EarthquakeEvent, ...]:
        try:
            with requests.get(
                url,
                headers={"User-Agent": EMSC_USER_AGENT},
                timeout=EMSC_REQUEST_SECONDS,
                stream=True,
            ) as response:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    status_code = response.status_code
                    raise FeedFetchError(
                        EMSC_LABEL,
                        "HTTPError",
                        status_code=status_code,
                        retryable=status_code == 429 or 500 <= status_code < 600,
                        retry_after=parse_retry_after(
                            response.headers.get("Retry-After"),
                        ),
                    ) from None
                content = self._read_content(response)
        except FeedFetchError:
            raise
        except (requests.ConnectionError, requests.Timeout) as error:
            raise FeedFetchError(
                EMSC_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(EMSC_LABEL, type(error).__name__) from None
        return _parse_events(content)

    @staticmethod
    def _read_content(response: requests.Response) -> bytes:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > MAX_EMSC_RESPONSE_BYTES:
                raise FeedFetchError(EMSC_LABEL, "ResponseTooLarge")

        content = bytearray()
        for chunk in response.iter_content(chunk_size=EMSC_STREAM_CHUNK_BYTES):
            if len(content) + len(chunk) > MAX_EMSC_RESPONSE_BYTES:
                raise FeedFetchError(EMSC_LABEL, "ResponseTooLarge")
            content.extend(chunk)
        return bytes(content)


def _parse_events(content: bytes) -> tuple[EarthquakeEvent, ...]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FeedFetchError(EMSC_LABEL, "InvalidResponse") from None
    if not isinstance(payload, Mapping):
        raise FeedFetchError(EMSC_LABEL, "InvalidResponse")
    features = payload.get("features")
    if not isinstance(features, list):
        raise FeedFetchError(EMSC_LABEL, "InvalidResponse")
    events = [
        event
        for feature in features[:MAX_EMSC_EVENTS]
        if (event := _parse_feature(feature)) is not None
    ]
    return tuple(reversed(events))


def _parse_feature(feature: object) -> EarthquakeEvent | None:
    if not isinstance(feature, Mapping):
        return None
    properties = feature.get("properties")
    if not isinstance(properties, Mapping):
        return None
    event_type = str(properties.get("evtype") or "").strip().casefold()
    if event_type and event_type not in EMSC_EVENT_TYPES:
        return None
    event_id = str(properties.get("unid") or feature.get("id") or "").strip()
    if not event_id:
        return None
    raw_time = str(properties.get("time") or "").strip()
    if not raw_time:
        return None
    try:
        timestamp = datetime.fromisoformat(raw_time)
    except ValueError:
        return None
    try:
        magnitude = float(properties["mag"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        depth_km = float(properties.get("depth") or 0.0)
    except (TypeError, ValueError):
        depth_km = 0.0
    latitude, longitude = _coordinates(feature, properties)
    return EarthquakeEvent(
        event_id=event_id,
        magnitude=magnitude,
        depth_km=depth_km,
        region=str(properties.get("flynn_region") or "").strip() or "Unknown region",
        time=timestamp.astimezone(UTC).isoformat(),
        latitude=latitude,
        longitude=longitude,
        author=str(properties.get("auth") or "").strip(),
        magnitude_type=str(properties.get("magtype") or "").strip(),
    )


def _coordinates(
    feature: Mapping[Any, Any],
    properties: Mapping[Any, Any],
) -> tuple[float, float]:
    try:
        return float(properties["lat"]), float(properties["lon"])
    except (KeyError, TypeError, ValueError):
        pass
    geometry = feature.get("geometry")
    if isinstance(geometry, Mapping):
        coordinates = geometry.get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) >= 2:
            try:
                return float(coordinates[1]), float(coordinates[0])
            except (TypeError, ValueError):
                pass
    return 0.0, 0.0
