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
        timeout_ms: int,
    ) -> Gjirafa50HttpResponse: ...

    def close(self) -> None: ...


class CurlCffiGjirafa50Response:
    def __init__(self, response: requests.Response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return {
            name.casefold(): value
            for name, value in self._response.headers.items()
            if value is not None
        }


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
        timeout_ms: int,
    ) -> Gjirafa50HttpResponse:
        session: requests.Session[requests.Response] = requests.Session(
            trust_env=False,
            discard_cookies=True,
            default_headers=False,
            curl_options={
                CurlOpt.TIMEOUT_MS: timeout_ms,
                CurlOpt.PROXY: "",
                CurlOpt.NETRC: 0,
                CurlOpt.CAINFO: certifi.where(),
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
            )
        finally:
            session.close()

    def close(self) -> None:
        """No persistent transfer session is retained."""


def create_gjirafa50_session() -> Gjirafa50HttpSession:
    return CurlCffiGjirafa50Session()
