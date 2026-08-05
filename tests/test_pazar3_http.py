import pytest

from rss2discord.transports import FeedFetchError, pazar3_http
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer
from tests.pazar3_helpers import RecordingGet, StubResponse, page_request, scan_budget


def fetch_with(
    monkeypatch: pytest.MonkeyPatch,
    get: RecordingGet,
    *,
    budget_bytes: int = 6_291_456,
) -> tuple[bytes, list[float]]:
    monkeypatch.setattr(pazar3_http, "_create_session", lambda: get)
    waits: list[float] = []
    pacer = Pazar3RequestPacer(lambda: 100.0)

    def record_sleep(seconds: float) -> bool:
        waits.append(seconds)
        return True

    return (
        pazar3_http.fetch_pazar3_page(
            page_request(),
            scan_budget(bytes_remaining=budget_bytes),
            pacer,
            record_sleep,
            lambda: False,
        ),
        waits,
    )


def test_pazar3_http_fetches_public_html_with_explicit_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b"<html>page</html>")])

    content, waits = fetch_with(monkeypatch, get)

    assert content == b"<html>page</html>"
    assert waits == []
    assert get.urls == [page_request().url]
    assert get.allow_redirects == [False]
    assert get.headers[0]["Accept"] == "text/html"


def test_pazar3_http_rejects_response_over_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [StubResponse(b"", chunks=(b"a" * 1_500_000, b"b" * 1_000_000))],
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        fetch_with(monkeypatch, get)

    assert fetch_error.value.cause_type == "ResponseTooLarge"


def test_pazar3_http_paces_every_redirect_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = page_request().url
    get = RecordingGet(
        [
            StubResponse(b"redirect", status_code=302, headers={"Location": target}),
            StubResponse(b"page", url=target),
        ],
    )
    now = [100.0]
    pacer = Pazar3RequestPacer(lambda: now[0])
    waits: list[float] = []

    def record_sleep(seconds: float) -> bool:
        waits.append(seconds)
        now[0] += seconds
        return True

    monkeypatch.setattr(pazar3_http, "_create_session", lambda: get)

    content = pazar3_http.fetch_pazar3_page(
        page_request(),
        scan_budget(),
        pacer,
        record_sleep,
        lambda: False,
    )

    assert content == b"page"
    assert waits == [20.0]
    assert get.urls == [target, target]
