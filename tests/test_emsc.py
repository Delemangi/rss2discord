"""EMSC earthquake strategy tests."""

import json
from collections.abc import Mapping

import pytest
from requests.structures import CaseInsensitiveDict

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.emsc import catalog as emsc_catalog
from rss2discord.providers.emsc.bounds import MAX_EMSC_RESPONSE_BYTES
from rss2discord.providers.emsc.catalog import EmscCatalogClient
from rss2discord.providers.emsc.strategy import EmscStrategy
from tests.fakes import StubResponse

EMSC_URL = "https://www.seismicportal.eu/fdsnws/event/1/query?format=json"


def feature(
    event_id: str,
    *,
    mag: float | None = 2.4,
    time: str = "2026-11-18T06:40:07.92Z",
    evtype: str = "ke",
    region: str = "GREECE",
    depth: float = 16.0,
    lat: float = 41.07,
    lon: float = 23.93,
) -> dict[str, object]:
    properties: dict[str, object] = {
        "time": time,
        "flynn_region": region,
        "lat": lat,
        "lon": lon,
        "depth": depth,
        "evtype": evtype,
        "auth": "EMSC",
        "magtype": "m",
        "unid": event_id,
    }
    if mag is not None:
        properties["mag"] = mag
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat, -depth]},
        "id": event_id,
        "properties": properties,
    }


def payload(features: list[dict[str, object]]) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": features}).encode()


def install(monkeypatch: pytest.MonkeyPatch, response: StubResponse) -> None:
    def get(
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
    ) -> StubResponse:
        del url, headers, timeout, stream
        return StubResponse(
            response.content,
            response.status_code,
            CaseInsensitiveDict(response.headers),
        )

    monkeypatch.setattr(emsc_catalog.requests, "get", get)


def test_emsc_filters_non_earthquakes_and_orders_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    install(
        monkeypatch,
        StubResponse(
            payload(
                [
                    feature("new", time="2026-11-18T08:00:00Z"),
                    feature("blast", evtype="qu", time="2026-11-18T07:00:00Z"),
                    feature("old", time="2026-11-18T06:00:00Z"),
                ],
            ),
        ),
    )

    # When
    events = EmscCatalogClient().fetch_events(EMSC_URL)

    # Then
    assert [event.event_id for event in events] == ["old", "new"]


def test_emsc_maps_event_to_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    install(monkeypatch, StubResponse(payload([feature("20261118_0000115")])))
    strategy = EmscStrategy()

    # When
    entries, title = strategy.fetch_entries(EMSC_URL)

    # Then
    assert title == "EMSC"
    assert strategy.seed_existing_on_first_fetch is True
    entry = entries[0]
    assert strategy.get_entry_id(entry) == "20261118_0000115"
    data = strategy.get_entry_data(entry)
    assert data.title == "M 2.4 — GREECE"
    assert data.link == (
        "https://www.seismicportal.eu/eventdetails.html?unid=20261118_0000115"
    )
    assert data.timestamp == "2026-11-18T06:40:07.920000+00:00"
    assert "Depth: 16 km" in data.description
    assert "Source: EMSC" in data.description
    assert data.categories == ("earthquake",)


def test_emsc_skips_malformed_features(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    install(
        monkeypatch,
        StubResponse(
            payload(
                [
                    feature("valid"),
                    feature("no-mag", mag=None),
                    feature("bad-time", time="not-a-time"),
                ],
            ),
        ),
    )

    # When
    events = EmscCatalogClient().fetch_events(EMSC_URL)

    # Then
    assert [event.event_id for event in events] == ["valid"]


def test_emsc_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    install(
        monkeypatch,
        StubResponse(
            b"{}",
            headers={"Content-Length": str(MAX_EMSC_RESPONSE_BYTES + 1)},
        ),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        EmscCatalogClient().fetch_events(EMSC_URL)


def test_emsc_maps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    install(monkeypatch, StubResponse(b"", status_code=503))

    # When / Then
    with pytest.raises(FeedFetchError) as error:
        EmscCatalogClient().fetch_events(EMSC_URL)
    assert error.value.cause_type == "HTTPError"
    assert error.value.status_code == 503
    assert error.value.retryable is True


def test_emsc_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    install(monkeypatch, StubResponse(b"not json"))

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        EmscCatalogClient().fetch_events(EMSC_URL)


def test_emsc_rejects_missing_features(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    install(monkeypatch, StubResponse(b'{"type":"FeatureCollection"}'))

    # When / Then
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        EmscCatalogClient().fetch_events(EMSC_URL)


def test_emsc_respects_ascending_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    install(
        monkeypatch,
        StubResponse(
            payload(
                [
                    feature("old", time="2026-11-18T06:00:00Z"),
                    feature("new", time="2026-11-18T08:00:00Z"),
                ],
            ),
        ),
    )

    # When
    events = EmscCatalogClient().fetch_events(
        f"{EMSC_URL}&orderby=time-asc",
    )

    # Then
    assert [event.event_id for event in events] == ["old", "new"]


def test_emsc_treats_naive_time_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    install(
        monkeypatch,
        StubResponse(
            payload([feature("naive", time="2026-11-18T06:40:07.92")]),
        ),
    )

    # When
    events = EmscCatalogClient().fetch_events(EMSC_URL)

    # Then
    assert events[0].time == "2026-11-18T06:40:07.920000+00:00"


def test_emsc_uses_geometry_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    item = feature("geometry")
    properties = item["properties"]
    assert isinstance(properties, dict)
    properties.pop("lat")
    properties.pop("lon")
    install(monkeypatch, StubResponse(payload([item])))

    # When
    events = EmscCatalogClient().fetch_events(EMSC_URL)

    # Then
    assert (events[0].latitude, events[0].longitude) == (41.07, 23.93)


def test_emsc_rejects_streamed_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    install(
        monkeypatch,
        StubResponse(b"x" * (MAX_EMSC_RESPONSE_BYTES + 1)),
    )

    # When / Then
    with pytest.raises(FeedFetchError, match="ResponseTooLarge"):
        EmscCatalogClient().fetch_events(EMSC_URL)


def test_emsc_formats_magnitude_with_one_decimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    install(monkeypatch, StubResponse(payload([feature("whole", mag=2.0)])))
    strategy = EmscStrategy()

    # When
    entries, _title = strategy.fetch_entries(EMSC_URL)

    # Then
    assert strategy.get_entry_data(entries[0]).title == "M 2.0 — GREECE"
