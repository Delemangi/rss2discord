from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import pytest
from curl_cffi import CurlOpt, requests

from rss2discord.transports import gjirafa50_session

type CurlOptionValue = int | str | Callable[[bytes], int]


@dataclass(frozen=True, slots=True)
class _Response:
    cookies: requests.Cookies
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)


@pytest.mark.parametrize(
    ("deletion_header", "advance_time"),
    [
        (b"Set-Cookie: gjs=; path=/; Max-Age=0\r\n", False),
        (
            b"Set-Cookie: gjs=stale; path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT\r\n",
            False,
        ),
        (b"Set-Cookie: gjs=ovh.sbg.win.web.44; path=/; Max-Age=1\r\n", True),
    ],
)
def test_session_forwards_and_deletes_valid_storefront_routing_cookie(
    monkeypatch: pytest.MonkeyPatch,
    deletion_header: bytes,
    advance_time: bool,
) -> None:
    captured_headers: list[Mapping[str, str]] = []
    request_count = 0
    monotonic_time = [0.0]
    monkeypatch.setattr(
        gjirafa50_session.time,
        "monotonic",
        lambda: monotonic_time[0],
    )

    class SessionStub:
        def __init__(
            self,
            curl_options: Mapping[CurlOpt, CurlOptionValue],
        ) -> None:
            self._curl_options = curl_options

        def get(
            self,
            url: str,
            *,
            params: Mapping[str, str | int],
            headers: Mapping[str, str],
            allow_redirects: bool,
            content_callback: Callable[[bytes], int],
            impersonate: str,
        ) -> _Response:
            nonlocal request_count
            del url, params, allow_redirects, impersonate
            captured_headers.append(headers)
            header_callback = self._curl_options[CurlOpt.HEADERFUNCTION]
            assert callable(header_callback)
            header_callback(b"HTTP/1.1 200 OK\r\n")
            if request_count == 0:
                header_callback(b"Set-Cookie: gjs=ovh.sbg.win.web.44; path=/\r\n")
                header_callback(b"Set-Cookie: untrusted=discard-me; path=/\r\n")
            elif request_count == 1:
                header_callback(deletion_header)
            header_callback(b"\r\n")
            content_callback(b"payload")
            cookies = requests.Cookies()
            request_count += 1
            return _Response(cookies)

        def close(self) -> None:
            return

    def create_session(
        *,
        trust_env: bool,
        discard_cookies: bool,
        default_headers: bool,
        curl_options: Mapping[CurlOpt, CurlOptionValue],
    ) -> SessionStub:
        assert trust_env is False
        assert discard_cookies is True
        assert default_headers is False
        return SessionStub(curl_options)

    monkeypatch.setattr(gjirafa50_session.requests, "Session", create_session)
    session = gjirafa50_session.create_gjirafa50_session()
    for request_index in range(3):
        session.get(
            "https://gjirafa50.mk/product/search",
            params={"pagenumber": 1},
            headers={"Accept": "application/json"},
            allow_redirects=False,
            content_callback=len,
            header_callback=len,
            timeout_ms=1_000,
        )
        if request_index == 1 and advance_time:
            monotonic_time[0] = 2.0

    assert captured_headers == [
        {"Accept": "application/json"},
        {
            "Accept": "application/json",
            "Cookie": "gjs=ovh.sbg.win.web.44",
        },
        {"Accept": "application/json"},
    ]


@pytest.mark.parametrize("value", ["bad;injected=true", "../escape", "UPPERCASE"])
def test_session_rejects_unsafe_routing_cookie(value: str) -> None:
    raw_lines = [
        b"HTTP/1.1 200 OK\r\n",
        f"Set-Cookie: gjs={value}; path=/\r\n".encode(),
        b"\r\n",
    ]

    assert gjirafa50_session._routing_cookie(raw_lines, "gjirafa50.mk") is None
