from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import certifi
import pytest
from curl_cffi import CurlOpt

from rss2discord.transports import reklama5_session


@dataclass(frozen=True, slots=True)
class SessionResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(
        default_factory=lambda: {"Content-Type": "text/html"},
    )
    url: str = "https://reklama5.mk/Search"


def test_reklama5_session_isolates_each_bounded_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_options: list[Mapping[CurlOpt, int | str]] = []
    captured_requests: list[tuple[bool, bytes]] = []
    closed: list[bool] = []

    class SessionStub:
        def get(
            self,
            url: str,
            *,
            headers: Mapping[str, str],
            allow_redirects: bool,
            content_callback: Callable[[bytes], int],
        ) -> SessionResponse:
            del url, headers
            chunk = b"page"
            content_callback(chunk)
            captured_requests.append((allow_redirects, chunk))
            return SessionResponse()

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

    monkeypatch.setattr(reklama5_session.requests, "Session", create_session)
    content = bytearray()

    def write_content(chunk: bytes) -> int:
        content.extend(chunk)
        return len(chunk)

    response = reklama5_session.create_reklama5_session().get(
        "https://reklama5.mk/Search",
        headers={"Accept": "text/html"},
        allow_redirects=False,
        content_callback=write_content,
        timeout_ms=5_000,
    )

    assert response.headers == {"content-type": "text/html"}
    assert captured_requests == [(False, b"page")]
    assert captured_options == [
        {
            CurlOpt.TIMEOUT_MS: 5_000,
            CurlOpt.PROXY: "",
            CurlOpt.NETRC: 0,
            CurlOpt.CAINFO: certifi.where(),
        },
    ]
    assert content == b"page"
    assert closed == [True]
