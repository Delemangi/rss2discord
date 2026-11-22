"""Air-quality provider behavior."""

import json

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.air_quality.bounds import AIR_QUALITY_URL
from rss2discord.providers.air_quality.client import AirQualityClient
from rss2discord.providers.air_quality.models import (
    AirQualityReading,
    classify_eaqi,
)
from rss2discord.providers.air_quality.strategy import AirQualityStrategy


def payload(**overrides: object) -> dict[str, object]:
    current: dict[str, object] = {
        "time": "2026-11-22T01:00",
        "interval": 3600,
        "european_aqi": 45,
        "pm10": 26.7,
        "pm2_5": 22.9,
        "us_aqi": 61,
    }
    current.update(overrides)
    return {
        "latitude": 42.0,
        "longitude": 21.4,
        "timezone": "Europe/Skopje",
        "timezone_abbreviation": "GMT+1",
        "current": current,
    }


def reading(
    *,
    observed_at: str = "2026-11-22T01:00",
    eaqi: float = 45,
) -> AirQualityReading:
    return AirQualityReading(
        observed_at=observed_at,
        timezone="Europe/Skopje",
        european_aqi=eaqi,
        pm10=26.7,
        pm2_5=22.9,
        us_aqi=61,
    )


def client_with(data: dict[str, object]) -> AirQualityClient:
    return AirQualityClient(
        fetch=lambda _: json.dumps(data).encode(),
    )


def test_client_parses_current_reading() -> None:
    result = client_with(payload()).fetch_reading(AIR_QUALITY_URL)

    assert result.observed_at == "2026-11-22T01:00"
    assert result.timezone == "Europe/Skopje"
    assert result.european_aqi == 45
    assert result.pm10 == 26.7
    assert result.pm2_5 == 22.9
    assert result.us_aqi == 61


@pytest.mark.parametrize(
    "url",
    [
        "http://air-quality-api.open-meteo.com/v1/air-quality",
        "https://example.com/v1/air-quality",
        "https://air-quality-api.open-meteo.com/other",
        "https://air-quality-api.open-meteo.com/v1/air-quality#fragment",
    ],
)
def test_client_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(FeedFetchError) as error:
        client_with(payload()).fetch_reading(url)

    assert error.value.cause_type == "InvalidUrl"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"current": None},
        payload(european_aqi=-1),
        payload(time=""),
    ],
)
def test_client_rejects_invalid_responses(data: dict[str, object]) -> None:
    with pytest.raises(FeedFetchError) as error:
        client_with(data).fetch_reading(AIR_QUALITY_URL)

    assert error.value.cause_type == "InvalidResponse"


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (0, "Good"),
        (20, "Good"),
        (21, "Fair"),
        (40, "Fair"),
        (41, "Moderate"),
        (60, "Moderate"),
        (61, "Poor"),
        (80, "Poor"),
        (81, "Very poor"),
        (100, "Very poor"),
        (101, "Extremely poor"),
    ],
)
def test_eaqi_bands(value: float, label: str) -> None:
    assert classify_eaqi(value).label == label


def test_entry_id_is_stable_per_day_and_category() -> None:
    strategy = AirQualityStrategy(client=client_with(payload()))

    assert strategy.get_entry_id(reading()) == "2026-11-22:moderate"
    assert strategy.get_entry_id(reading(eaqi=50)) == "2026-11-22:moderate"
    assert strategy.get_entry_id(reading(eaqi=75)) == "2026-11-22:poor"
    assert (
        strategy.get_entry_id(reading(observed_at="2026-11-23T01:00"))
        == "2026-11-23:moderate"
    )


def test_entry_data_contains_air_quality_metrics() -> None:
    strategy = AirQualityStrategy(client=client_with(payload()))

    data = strategy.get_entry_data(reading())

    assert data.title == "Air quality: Moderate (EAQI 45)"
    assert data.link == "https://open-meteo.com/en/docs/air-quality-api"
    assert data.author == "Open-Meteo / CAMS"
    assert "PM2.5: 22.9 µg/m³" in data.description
    assert "Observed: 2026-11-22 01:00 (Europe/Skopje)" in data.description
    assert [metric.label for metric in data.source_metrics] == [
        "EAQI",
        "PM2.5",
        "PM10",
        "US AQI",
        "Category",
    ]


def test_config_accepts_air_quality_strategy() -> None:
    feed = FeedConfig(
        id="air-quality-skopje",
        url=AIR_QUALITY_URL,
        webhook="https://discord.test/webhook",
        strategy="air_quality",
    )

    assert feed.strategy == "air_quality"
