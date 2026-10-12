"""Isolated curl-cffi session boundary for Gjirafa50 transfers."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, SimpleCookie
from typing import Final, Protocol, assert_never
from urllib.parse import urlsplit

import certifi
from curl_cffi import CurlOpt, requests

GJIRAFA50_ROUTING_COOKIE_PATTERN: Final = re.compile(r"[a-z0-9.-]{1,128}")


@dataclass(frozen=True, slots=True)
class _SetRoutingCookie:
    value: str
    expires_at: float | None


@dataclass(frozen=True, slots=True)
class _DeleteRoutingCookie:
    pass


type _RoutingCookieDirective = _SetRoutingCookie | _DeleteRoutingCookie | None


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

    def __init__(self) -> None:
        self._routing_cookies: dict[str, _SetRoutingCookie] = {}

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
        hostname = urlsplit(url).hostname
        request_headers = {
            name: value
            for name, value in headers.items()
            if name.casefold() != "cookie"
        }
        if (
            hostname is not None
            and (routing_cookie := self._routing_cookies.get(hostname)) is not None
        ):
            if (
                routing_cookie.expires_at is not None
                and time.monotonic() >= routing_cookie.expires_at
            ):
                self._routing_cookies.pop(hostname, None)
            else:
                request_headers["Cookie"] = f"gjs={routing_cookie.value}"

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
            response = session.get(
                url,
                params=dict(params),
                headers=request_headers,
                allow_redirects=allow_redirects,
                content_callback=content_callback,
                impersonate="chrome",
            )
            if hostname is not None:
                match _routing_cookie(raw_header_lines, hostname):
                    case _SetRoutingCookie() as set_cookie:
                        self._routing_cookies[hostname] = set_cookie
                    case _DeleteRoutingCookie():
                        self._routing_cookies.pop(hostname, None)
                    case None:
                        pass
                    case unreachable:
                        assert_never(unreachable)
            return CurlCffiGjirafa50Response(response, raw_header_lines)
        finally:
            session.close()

    def close(self) -> None:
        self._routing_cookies.clear()


def create_gjirafa50_session() -> Gjirafa50HttpSession:
    return CurlCffiGjirafa50Session()


def _routing_cookie(
    raw_lines: list[bytes],
    hostname: str,
) -> _RoutingCookieDirective:
    values: list[str] = []
    for raw_line in raw_lines:
        line = raw_line.rstrip(b"\r\n")
        if line.startswith(b"HTTP/"):
            values = []
        elif b":" in line:
            raw_name, raw_value = line.split(b":", 1)
            if raw_name.strip().lower() == b"set-cookie":
                values.append(raw_value.decode("latin-1").strip())
    directive: _RoutingCookieDirective = None
    for value in values:
        cookie = SimpleCookie()
        try:
            cookie.load(value)
        except CookieError:
            continue
        routing_cookie = cookie.get("gjs")
        if routing_cookie is None:
            continue
        domain = routing_cookie["domain"]
        if routing_cookie["path"] != "/" or domain not in {
            "",
            hostname,
            f".{hostname}",
        }:
            continue
        max_age = routing_cookie["max-age"]
        expiry_deadline: float | None = None
        if max_age:
            try:
                max_age_seconds = int(max_age)
            except ValueError:
                continue
            if max_age_seconds <= 0:
                directive = _DeleteRoutingCookie()
                continue
            expiry_deadline = time.monotonic() + max_age_seconds
        elif expires := routing_cookie["expires"]:
            try:
                expires_at = parsedate_to_datetime(expires)
            except (OverflowError, TypeError, ValueError):
                continue
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            remaining_seconds = (expires_at - datetime.now(UTC)).total_seconds()
            if remaining_seconds <= 0:
                directive = _DeleteRoutingCookie()
                continue
            expiry_deadline = time.monotonic() + remaining_seconds
        if GJIRAFA50_ROUTING_COOKIE_PATTERN.fullmatch(routing_cookie.value) is not None:
            directive = _SetRoutingCookie(routing_cookie.value, expiry_deadline)
    return directive


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
