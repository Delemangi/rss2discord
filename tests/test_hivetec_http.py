import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from curl_cffi import CurlError
from curl_cffi.const import CurlECode

from rss2discord.transports.hivetec_transport import _perform_request


class _SlowTrickleHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        try:
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.02)
        except BrokenPipeError:
            pass

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def test_hivetec_transport_stops_slow_trickle_at_total_timeout() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowTrickleHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    callback: Callable[[bytes], int] = len
    started_at = time.monotonic()
    try:
        with pytest.raises(CurlError) as raised:
            _perform_request(
                f"http://127.0.0.1:{server.server_port}/",
                timeout_ms=100,
                header_callback=callback,
                content_callback=callback,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert raised.value.code == CurlECode.OPERATION_TIMEDOUT
    assert time.monotonic() - started_at < 1
