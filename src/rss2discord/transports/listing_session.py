"""Isolated curl-cffi session boundary for bounded listing transfers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

import certifi
from curl_cffi import CurlOpt, requests


class ListingHttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def url(self) -> str: ...


class ListingHttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> ListingHttpResponse: ...

    def close(self) -> None: ...


class CurlCffiListingResponse:
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


class CurlCffiListingSession:
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        allow_redirects: bool,
        content_callback: Callable[[bytes], int],
        timeout_ms: int,
    ) -> ListingHttpResponse:
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
            return CurlCffiListingResponse(
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


def create_listing_session() -> ListingHttpSession:
    return CurlCffiListingSession()
