"""Air-quality API models and EAQI classification."""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


class _AirQualityModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class AirQualityCurrent(_AirQualityModel):
    time: str
    european_aqi: float
    pm10: float
    pm2_5: float
    us_aqi: float


class AirQualityResponse(_AirQualityModel):
    current: AirQualityCurrent
    timezone: str = "Europe/Skopje"
    timezone_abbreviation: str | None = None


@dataclass(frozen=True, slots=True)
class AirQualityReading:
    """One current air-quality observation."""

    observed_at: str
    timezone: str
    european_aqi: float
    pm10: float
    pm2_5: float
    us_aqi: float


@dataclass(frozen=True, slots=True)
class AirQualityCategory:
    slug: str
    label: str


def classify_eaqi(value: float) -> AirQualityCategory:
    """Map a European AQI value to the EEA band."""
    if value <= 20:
        return AirQualityCategory("good", "Good")
    if value <= 40:
        return AirQualityCategory("fair", "Fair")
    if value <= 60:
        return AirQualityCategory("moderate", "Moderate")
    if value <= 80:
        return AirQualityCategory("poor", "Poor")
    if value <= 100:
        return AirQualityCategory("very-poor", "Very poor")
    return AirQualityCategory("extremely-poor", "Extremely poor")
