"""Isolated curl-cffi session boundary for Reklama5 transfers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

import certifi
from curl_cffi import CurlOpt, requests


class Reklama5HttpResponse(Protocol):
    """The curl-cffi response fields used by Reklama5."""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def url(self) -> str: ...


class Reklama5HttpSession(Protocol):
    """The isolated curl-cffi session capability used by Reklama5."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> Reklama5HttpResponse: ...

    def close(self) -> None: ...


class CurlCffiReklama5Response:
    """Expose curl-cffi response metadata through the narrow protocol."""

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

    @property
    def url(self) -> str:
        return self._response.url


class CurlCffiReklama5Session:
    """Create one environment-isolated curl session per bounded transfer."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> Reklama5HttpResponse:
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
            return CurlCffiReklama5Response(
                session.get(
                    url,
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


def create_reklama5_session() -> Reklama5HttpSession:
    """Return the production Reklama5 transfer session."""
    return CurlCffiReklama5Session()
