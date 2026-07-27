import math

import pytest
from curl_cffi import requests

from rss2discord.transports import FeedFetchError
from rss2discord.transports.neksio_catalog_http import (
    NEKSIO_ORIGIN,
    NeksioScanBudget,
    fetch_homepage,
)
from tests.neksio_helpers import RecordingGet, StubResponse


def test_scan_budget_rejects_a_response_before_sending_past_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b"first"), StubResponse(b"second")])
    budget = NeksioScanBudget(
        responses_remaining=1,
        bytes_remaining=100,
        expires_at=math.inf,
    )
    monkeypatch.setattr(requests, "get", get)

    # When
    assert fetch_homepage(NEKSIO_ORIGIN, budget=budget) == b"first"

    # Then
    with pytest.raises(FeedFetchError, match="ScanResponseLimitExceeded"):
        fetch_homepage(NEKSIO_ORIGIN, budget=budget)
    assert len(get.urls) == 1


def test_scan_budget_rejects_cumulative_response_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b"12345")])
    budget = NeksioScanBudget(
        responses_remaining=1,
        bytes_remaining=4,
        expires_at=math.inf,
    )
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanByteLimitExceeded"):
        fetch_homepage(NEKSIO_ORIGIN, budget=budget)


def test_scan_budget_rejects_an_expired_scan_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    get = RecordingGet([StubResponse(b"unused")])
    budget = NeksioScanBudget(responses_remaining=1, bytes_remaining=100, expires_at=0)
    monkeypatch.setattr(requests, "get", get)

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        fetch_homepage(NEKSIO_ORIGIN, budget=budget)
    assert get.urls == []


def test_scan_budget_rejects_streamed_bytes_after_the_deadline() -> None:
    # Given
    budget = NeksioScanBudget(responses_remaining=1, bytes_remaining=100, expires_at=0)

    # When / Then
    with pytest.raises(FeedFetchError, match="ScanTimeLimitExceeded"):
        budget.consume_bytes(1)
