"""Transient curl transport failures that callers may retry."""

from typing import Final

from curl_cffi.const import CurlECode

TRANSIENT_CURL_ERRORS: Final = frozenset(
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
