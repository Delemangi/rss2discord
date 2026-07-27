from collections.abc import Mapping

import pytest
from curl_cffi import CurlOpt, requests

from rss2discord.transports import neksio_catalog_http
from rss2discord.transports.neksio_catalog_http import (
    NEKSIO_ORIGIN,
    NeksioScanBudget,
    fetch_homepage,
)
from tests.neksio_helpers import StubResponse


class _CurlGetRecorder:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.timeout_options: list[int] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
        allow_redirects: bool,
        curl_options: Mapping[CurlOpt, int],
    ) -> StubResponse:
        del url, headers, timeout, stream, allow_redirects
        timeout_ms = curl_options[CurlOpt.TIMEOUT_MS]
        self.timeout_options.append(timeout_ms)
        return StubResponse(self._content)


def test_fetch_homepage_uses_a_hard_total_transfer_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    curl_get = _CurlGetRecorder(b"homepage")
    budget = NeksioScanBudget(responses_remaining=1, bytes_remaining=100, expires_at=15)
    monkeypatch.setattr(requests, "get", curl_get)
    monkeypatch.setattr(neksio_catalog_http.time, "monotonic", lambda: 10)

    # When
    content = fetch_homepage(NEKSIO_ORIGIN, budget=budget)

    # Then
    assert content == b"homepage"
    assert curl_get.timeout_options == [5000]
