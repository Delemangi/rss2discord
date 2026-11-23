"""Bounded EMSC FDSN event traversal."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
from rss2discord.transports.bounded_http import read_bounded_content


class EmscCatalogClient:
    """Fetch one bounded EMSC FDSN event query."""

    def fetch_events(self, url: str) -> tuple[EarthquakeEvent, ...]:
        reverse = _is_descending_order(url)
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
                content = read_bounded_content(
                    response,
                    label=EMSC_LABEL,
                    max_bytes=MAX_EMSC_RESPONSE_BYTES,
                    chunk_bytes=EMSC_STREAM_CHUNK_BYTES,
                )
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
        return _parse_events(content, reverse=reverse)


def _parse_events(
    content: bytes,
    *,
    reverse: bool,
) -> tuple[EarthquakeEvent, ...]:
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
    return tuple(reversed(events)) if reverse else tuple(events)


def _is_descending_order(url: str) -> bool:
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return True
    order = (query.get("orderby") or ["time"])[0].strip().casefold()
    return order not in {"time-asc", "magnitude-asc"}


def _parse_feature(feature: object) -> EarthquakeEvent | None:
    if not isinstance(feature, Mapping):
        return None
    properties = feature.get("properties")
    if not isinstance(properties, Mapping):
        return None
    if not _has_supported_event_type(properties):
        return None
    identity = _event_identity(feature, properties)
    if identity is None:
        return None
    event_id, timestamp = identity
    magnitude = _magnitude(properties)
    if magnitude is None:
        return None
    latitude, longitude = _coordinates(feature, properties)
    return EarthquakeEvent(
        event_id=event_id,
        magnitude=magnitude,
        depth_km=_depth_km(properties),
        region=str(properties.get("flynn_region") or "").strip() or "Unknown region",
        time=timestamp.astimezone(UTC).isoformat(),
        latitude=latitude,
        longitude=longitude,
        author=str(properties.get("auth") or "").strip(),
        magnitude_type=str(properties.get("magtype") or "").strip(),
    )


def _has_supported_event_type(properties: Mapping[Any, Any]) -> bool:
    event_type = str(properties.get("evtype") or "").strip().casefold()
    return not event_type or event_type in EMSC_EVENT_TYPES


def _event_identity(
    feature: Mapping[Any, Any],
    properties: Mapping[Any, Any],
) -> tuple[str, datetime] | None:
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
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return event_id, timestamp


def _magnitude(properties: Mapping[Any, Any]) -> float | None:
    try:
        return float(properties["mag"])
    except (KeyError, TypeError, ValueError):
        return None


def _depth_km(properties: Mapping[Any, Any]) -> float:
    try:
        return float(properties.get("depth") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
