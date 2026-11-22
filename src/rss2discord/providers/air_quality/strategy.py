"""Open-Meteo current air-quality discovery strategy."""

from datetime import UTC, datetime
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.air_quality.bounds import AIR_QUALITY_LABEL
from rss2discord.providers.air_quality.client import AirQualityClient
from rss2discord.providers.air_quality.models import (
    AirQualityReading,
    classify_eaqi,
)
from rss2discord.transports.base import ScraperStrategy

AIR_QUALITY_LINK = "https://open-meteo.com/en/docs/air-quality-api"


@final
class AirQualityStrategy(ScraperStrategy):
    """Discover one current air-quality reading per day/category."""

    seed_existing_on_first_fetch = True

    def __init__(self, client: AirQualityClient | None = None) -> None:
        self._client = client or AirQualityClient()

    @override
    def fetch_entries(self, url: str) -> tuple[list[AirQualityReading], str]:
        return [self._client.fetch_reading(url)], AIR_QUALITY_LABEL

    @override
    def get_entry_id(self, entry: AirQualityReading) -> EntryId:
        category = classify_eaqi(entry.european_aqi)
        return EntryId(f"{_observed_date(entry)}:{category.slug}")

    @override
    def get_entry_data(self, entry: AirQualityReading) -> EntryData:
        category = classify_eaqi(entry.european_aqi)
        return EntryData(
            title=(
                f"Air quality: {category.label} "
                f"(EAQI {_format_eaqi(entry.european_aqi)})"
            ),
            link=AIR_QUALITY_LINK,
            description="\n".join(
                (
                    f"PM2.5: {entry.pm2_5:.1f} µg/m³",
                    f"PM10: {entry.pm10:.1f} µg/m³",
                    f"US AQI: {entry.us_aqi:.0f}",
                    f"Observed: {_format_observed(entry)}",
                    "Source: Open-Meteo / CAMS",
                ),
            ),
            author="Open-Meteo / CAMS",
            timestamp=datetime.now(UTC).isoformat(),
            categories=("air-quality",),
            source_metrics=(
                SourceMetric(
                    label="EAQI",
                    value=_format_eaqi(entry.european_aqi),
                ),
                SourceMetric(label="PM2.5", value=f"{entry.pm2_5:.1f} µg/m³"),
                SourceMetric(label="PM10", value=f"{entry.pm10:.1f} µg/m³"),
                SourceMetric(label="US AQI", value=f"{entry.us_aqi:.0f}"),
                SourceMetric(label="Category", value=category.label),
            ),
        )


def _observed_date(entry: AirQualityReading) -> str:
    date = entry.observed_at[:10]
    if len(date) != 10:
        return datetime.now(UTC).date().isoformat()
    return date


def _format_eaqi(value: float) -> str:
    return f"{value:.0f}" if value.is_integer() else f"{value:.1f}"


def _format_observed(entry: AirQualityReading) -> str:
    observed = entry.observed_at.replace("T", " ")
    return f"{observed} ({entry.timezone})"
