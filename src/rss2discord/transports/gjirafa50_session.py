"""Isolated curl-cffi session boundary for Gjirafa50 transfers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

import certifi
from curl_cffi import CurlOpt, requests


class Gjirafa50HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


class Gjirafa50HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        header_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> Gjirafa50HttpResponse: ...

    def close(self) -> None: ...


class CurlCffiGjirafa50Response:
    def __init__(
        self,
        response: requests.Response,
        raw_header_lines: list[bytes],
    ) -> None:
        self._response = response
        self._headers = _parse_final_headers(raw_header_lines)

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._headers


class CurlCffiGjirafa50Session:
    """Create one environment-isolated curl session per bounded transfer."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        header_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> Gjirafa50HttpResponse:
        raw_header_lines: list[bytes] = []

        def receive_header(line: bytes) -> int:
            consumed = header_callback(line)
            if consumed == len(line):
                raw_header_lines.append(line)
            return consumed

        session: requests.Session[requests.Response] = requests.Session(
            trust_env=False,
            discard_cookies=True,
            default_headers=False,
            curl_options={
                CurlOpt.TIMEOUT_MS: timeout_ms,
                CurlOpt.PROXY: "",
                CurlOpt.NETRC: 0,
                CurlOpt.CAINFO: certifi.where(),
                CurlOpt.HEADERFUNCTION: receive_header,
            },
        )
        try:
            return CurlCffiGjirafa50Response(
                session.get(
                    url,
                    params=dict(params),
                    headers=headers,
                    allow_redirects=allow_redirects,
                    content_callback=content_callback,
                    impersonate="chrome",
                ),
                raw_header_lines,
            )
        finally:
            session.close()

    def close(self) -> None:
        """No persistent transfer session is retained."""


def create_gjirafa50_session() -> Gjirafa50HttpSession:
    return CurlCffiGjirafa50Session()


def _parse_final_headers(raw_lines: list[bytes]) -> Mapping[str, str]:
    headers: dict[str, str] = {}
    last_name: str | None = None
    for raw_line in raw_lines:
        line = raw_line.rstrip(b"\r\n")
        if line.startswith(b"HTTP/"):
            headers = {}
            last_name = None
        elif not line:
            continue
        elif line[:1] in {b" ", b"\t"} and last_name is not None:
            headers[last_name] += " " + line.decode("latin-1").strip()
        elif b":" in line:
            raw_name, raw_value = line.split(b":", 1)
            last_name = raw_name.decode("latin-1").strip().casefold()
            headers[last_name] = raw_value.decode("latin-1").strip()
    return headers
