from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import certifi
import pytest
from curl_cffi import CurlOpt

from rss2discord.transports import listing_session


@dataclass(frozen=True, slots=True)
class SessionResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(
        default_factory=lambda: {"Content-Type": "text/html"},
    )
    url: str = "https://listing.example.test/search"


def test_listing_session_isolates_each_bounded_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_options: list[Mapping[CurlOpt, int | str]] = []
    captured_requests: list[tuple[str, bool, str, bytes]] = []
    closed: list[bool] = []

    class SessionStub:
        def get(
            self,
            url: str,
            *,
            headers: Mapping[str, str],
            allow_redirects: bool,
            content_callback: Callable[[bytes], int],
            impersonate: str,
        ) -> SessionResponse:
            del headers
            chunk = url.encode()
            content_callback(chunk)
            captured_requests.append((url, allow_redirects, impersonate, chunk))
            return SessionResponse(url=url)

        def close(self) -> None:
            closed.append(True)

    def create_session(
        *,
        trust_env: bool,
        discard_cookies: bool,
        default_headers: bool,
        curl_options: Mapping[CurlOpt, int | str],
    ) -> SessionStub:
        assert not trust_env
        assert discard_cookies
        assert not default_headers
        captured_options.append(curl_options)
        return SessionStub()

    monkeypatch.setattr(listing_session.requests, "Session", create_session)
    received: list[bytes] = []

    def write_content(chunk: bytes) -> int:
        received.append(chunk)
        return len(chunk)

    session = listing_session.create_listing_session()
    responses = [
        session.get(
            "https://pazar3.example.test/search",
            headers={"Accept": "text/html"},
            allow_redirects=False,
            content_callback=write_content,
            timeout_ms=5_000,
        ),
        session.get(
            "https://reklama5.example.test/search",
            headers={"Accept": "text/html"},
            allow_redirects=True,
            content_callback=write_content,
            timeout_ms=7_000,
        ),
    ]

    assert [response.url for response in responses] == [
        "https://pazar3.example.test/search",
        "https://reklama5.example.test/search",
    ]
    assert [response.headers for response in responses] == [
        {"content-type": "text/html"},
        {"content-type": "text/html"},
    ]
    assert captured_requests == [
        (
            "https://pazar3.example.test/search",
            False,
            "chrome",
            b"https://pazar3.example.test/search",
        ),
        (
            "https://reklama5.example.test/search",
            True,
            "chrome",
            b"https://reklama5.example.test/search",
        ),
    ]
    assert captured_options == [
        {
            CurlOpt.TIMEOUT_MS: 5_000,
            CurlOpt.PROXY: "",
            CurlOpt.NETRC: 0,
            CurlOpt.CAINFO: certifi.where(),
        },
        {
            CurlOpt.TIMEOUT_MS: 7_000,
            CurlOpt.PROXY: "",
            CurlOpt.NETRC: 0,
            CurlOpt.CAINFO: certifi.where(),
        },
    ]
    assert received == [
        b"https://pazar3.example.test/search",
        b"https://reklama5.example.test/search",
    ]
    assert closed == [True, True]


def test_listing_session_closes_underlying_session_when_transfer_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class FailingSession:
        def get(
            self,
            url: str,
            *,
            headers: Mapping[str, str],
            allow_redirects: bool,
            content_callback: Callable[[bytes], int],
            impersonate: str,
        ) -> SessionResponse:
            del url, headers, allow_redirects, content_callback, impersonate
            raise OSError("transfer failed")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        listing_session.requests,
        "Session",
        lambda **options: FailingSession(),
    )

    with pytest.raises(OSError, match="transfer failed"):
        listing_session.create_listing_session().get(
            "https://listing.example.test/search",
            headers={},
            allow_redirects=False,
            content_callback=len,
            timeout_ms=5_000,
        )

    assert closed == [True]
