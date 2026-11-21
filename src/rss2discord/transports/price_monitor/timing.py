"""Full-scan timing context shared by price-monitor entrypoints."""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_SCAN_STARTED_AT: ContextVar[float | None] = ContextVar(
    "price_scan_started_at",
    default=None,
)


def current_scan_started_at() -> float | None:
    """Return the active provider scan start, if one is in progress."""
    return _SCAN_STARTED_AT.get()


@contextmanager
def price_scan_timing() -> Iterator[None]:
    """Measure a provider scan from the runtime or worker entrypoint."""
    token = _SCAN_STARTED_AT.set(time.perf_counter())
    try:
        yield
    finally:
        _SCAN_STARTED_AT.reset(token)
