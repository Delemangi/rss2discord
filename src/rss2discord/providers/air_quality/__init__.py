"""Open-Meteo air-quality provider."""

from .client import AirQualityClient
from .models import AirQualityReading, classify_eaqi
from .strategy import AirQualityStrategy

__all__ = [
    "AirQualityClient",
    "AirQualityReading",
    "AirQualityStrategy",
    "classify_eaqi",
]
