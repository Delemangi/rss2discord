"""Isolated curl-cffi session boundary for DDStore transfers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

import certifi
from curl_cffi import CurlOpt, requests
from pydantic import JsonValue


class DDStoreHttpResponse(Protocol):
    """The curl-cffi response fields used by DDStore."""

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...


class DDStoreHttpSession(Protocol):
    """The isolated curl-cffi session capability used by DDStore."""

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> DDStoreHttpResponse: ...

    def close(self) -> None: ...


class CurlCffiDDStoreResponse:
    """Expose curl-cffi response metadata through the narrow transport protocol."""

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


class CurlCffiDDStoreSession:
    """Create one environment-isolated curl session per bounded transfer."""

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> DDStoreHttpResponse:
        session: requests.Session[requests.Response] = requests.Session(
            trust_env=False,
            discard_cookies=True,
            impersonate="chrome",
            default_headers=False,
            curl_options={
                CurlOpt.TIMEOUT_MS: timeout_ms,
                CurlOpt.PROXY: "",
                CurlOpt.NETRC: 0,
                CurlOpt.CAINFO: certifi.where(),
            },
        )
        try:
            return CurlCffiDDStoreResponse(
                session.post(
                    url,
                    json=dict(json),
                    headers=headers,
                    allow_redirects=allow_redirects,
                    content_callback=content_callback,
                ),
            )
        finally:
            session.close()

    def close(self) -> None:
        """No persistent transfer session is retained."""


def create_ddstore_session() -> DDStoreHttpSession:
    """Return the production DDStore transfer session."""
    return CurlCffiDDStoreSession()
