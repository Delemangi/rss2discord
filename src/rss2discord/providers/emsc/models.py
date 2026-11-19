"""EMSC earthquake value objects."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EarthquakeEvent:
    """One EMSC earthquake event."""

    event_id: str
    magnitude: float
    depth_km: float
    region: str
    time: str
    latitude: float
    longitude: float
    author: str
    magnitude_type: str
