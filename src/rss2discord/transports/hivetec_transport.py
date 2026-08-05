"""Bounded curl transport for Hivetec API responses."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from curl_cffi import Curl, CurlError, CurlInfo, CurlOpt
from curl_cffi.const import CurlECode
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.hivetec_bounds import (
    HIVETEC_LABEL,
    HIVETEC_USER_AGENT,
    HivetecPageRequest,
)
from rss2discord.transports.hivetec_budget import HivetecScanBudget

TRANSIENT_HIVETEC_CURL_ERRORS = frozenset(
    {
        CurlECode.COULDNT_RESOLVE_HOST,
        CurlECode.COULDNT_CONNECT,
        CurlECode.PARTIAL_FILE,
        CurlECode.OPERATION_TIMEDOUT,
        CurlECode.GOT_NOTHING,
        CurlECode.SEND_ERROR,
        CurlECode.RECV_ERROR,
        CurlECode.AGAIN,
        CurlECode.NO_CONNECTION_AVAILABLE,
        CurlECode.HTTP2,
        CurlECode.HTTP2_STREAM,
        CurlECode.HTTP3,
        CurlECode.QUIC_CONNECT_ERROR,
    },
)
type TransferCallback = Callable[[bytes], int]


@dataclass(frozen=True, slots=True)
class HivetecCurlResponse:
    status_code: int
    url: str


@dataclass(frozen=True, slots=True)
class HivetecTransportResponse:
    content: bytes
    headers: Mapping[str, str]
    status_code: int
    url: str


class _BoundedHivetecResponse:
    """Collect one response while charging headers and body to fixed limits."""

    def __init__(
        self,
        request: HivetecPageRequest,
        budget: HivetecScanBudget,
    ) -> None:
        self.content = bytearray()
        self.headers: dict[str, str] = {}
        self.failure: BaseException | None = None
        self._budget = budget
        self._current_headers: dict[str, str] = {}
        self._max_response_bytes = request.max_response_bytes
        self._response_bytes = 0

    def write_header(self, chunk: bytes) -> int:
        if not self._consume_transfer_bytes(chunk):
            return CURL_WRITEFUNC_ERROR
        line = chunk.rstrip(b"\r\n")
        if line.startswith(b"HTTP/"):
            self._current_headers = {}
        elif not line:
            self.headers = self._current_headers.copy()
            try:
                self._require_declared_content_capacity()
            except FeedFetchError as error:
                self.failure = error
                return CURL_WRITEFUNC_ERROR
        elif b":" in line:
            name, value = line.split(b":", 1)
            try:
                self._current_headers[name.decode("ascii").strip().lower()] = (
                    value.decode("latin-1").strip()
                )
            except UnicodeDecodeError:
                self.failure = FeedFetchError(HIVETEC_LABEL, "InvalidResponse")
                return CURL_WRITEFUNC_ERROR
        return len(chunk)

    def write_content(self, chunk: bytes) -> int:
        if not self._consume_transfer_bytes(chunk):
            return CURL_WRITEFUNC_ERROR
        self.content.extend(chunk)
        return len(chunk)

    def _require_declared_content_capacity(self) -> None:
        content_length = self.headers.get("content-length")
        if content_length is None:
            return
        try:
            declared_bytes = int(content_length)
        except ValueError:
            raise FeedFetchError(HIVETEC_LABEL, "InvalidResponse") from None
        if declared_bytes < 0 or self._response_bytes + declared_bytes > (
            self._max_response_bytes
        ):
            raise FeedFetchError(HIVETEC_LABEL, "ResponseTooLarge")
        self._budget.require_byte_capacity(declared_bytes)

    def _consume_transfer_bytes(self, chunk: bytes) -> bool:
        if self._response_bytes + len(chunk) > self._max_response_bytes:
            self.failure = FeedFetchError(HIVETEC_LABEL, "ResponseTooLarge")
            return False
        try:
            self._budget.consume_bytes(len(chunk))
        except (FeedFetchError, FeedFetchInterruptedError) as error:
            self.failure = error
            return False
        self._response_bytes += len(chunk)
        return True


class HivetecTransport:
    def __init__(self, budget: HivetecScanBudget) -> None:
        self._budget = budget

    def fetch(
        self,
        url: str,
        request: HivetecPageRequest,
    ) -> HivetecTransportResponse:
        collected = _BoundedHivetecResponse(request, self._budget)
        try:
            response = _perform_request(
                url,
                timeout_ms=self._budget.transfer_timeout_ms(),
                header_callback=collected.write_header,
                content_callback=collected.write_content,
            )
        except CurlError as error:
            if collected.failure is not None:
                raise collected.failure from None
            if error.code == CurlECode.OPERATION_TIMEDOUT:
                self._budget.remaining_seconds()
            raise FeedFetchError(
                HIVETEC_LABEL,
                type(error).__name__,
                retryable=error.code in TRANSIENT_HIVETEC_CURL_ERRORS,
            ) from None
        if collected.failure is not None:
            raise collected.failure
        self._budget.remaining_seconds()
        return HivetecTransportResponse(
            content=bytes(collected.content),
            headers=collected.headers,
            status_code=response.status_code,
            url=response.url,
        )


def _perform_request(
    url: str,
    *,
    timeout_ms: int,
    header_callback: TransferCallback,
    content_callback: TransferCallback,
) -> HivetecCurlResponse:
    curl = Curl()
    try:
        curl.setopt(CurlOpt.URL, url.encode())
        curl.setopt(
            CurlOpt.HTTPHEADER,
            [
                b"Accept: application/json",
                f"User-Agent: {HIVETEC_USER_AGENT}".encode(),
            ],
        )
        curl.setopt(CurlOpt.FOLLOWLOCATION, 0)
        curl.setopt(CurlOpt.CONNECTTIMEOUT_MS, min(timeout_ms, 5_000))
        curl.setopt(CurlOpt.TIMEOUT_MS, timeout_ms)
        curl.setopt(CurlOpt.HEADERFUNCTION, header_callback)
        curl.setopt(CurlOpt.WRITEFUNCTION, content_callback)
        curl.perform()
        status_code = curl.getinfo(CurlInfo.RESPONSE_CODE)
        effective_url = curl.getinfo(CurlInfo.EFFECTIVE_URL)
        if not isinstance(status_code, int) or not isinstance(effective_url, bytes):
            raise FeedFetchError(HIVETEC_LABEL, "InvalidResponse")
        return HivetecCurlResponse(
            status_code=status_code,
            url=effective_url.decode("utf-8"),
        )
    finally:
        curl.close()
